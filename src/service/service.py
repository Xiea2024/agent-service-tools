"""API 层：使用 FastAPI 接收请求，调用 LangGraph Agent 并转换响应。

Pydantic 定义协议，LangGraph 管理执行与 checkpoint，SSE 承载流式消息和文本片段。
可选的 Langfuse 回调用于追踪，LangSmith 接口用于反馈；启用情况取决于配置。
"""

import asyncio
import inspect
import json
import logging
import warnings
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

from anyio import CancelScope
from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Interrupt
from langsmith import Client as LangsmithClient
from langsmith import uuid7

from agents import DEFAULT_AGENT, AgentGraph, get_agent, get_all_agent_info, load_agent
from core import settings
from memory import initialize_database, initialize_store
from observability.identifiers import trace_id_for
from observability.tracing import (
    get_tracing_client,
    shutdown_tracing,
    trace_metadata,
    tracing_callback,
    tracing_status,
)
from schema import (
    ChatHistory,
    ChatHistoryInput,
    ChatMessage,
    Feedback,
    FeedbackResponse,
    ServiceMetadata,
    StreamInput,
    UserInput,
    UserThreads,
    UserThreadsInput,
)
from service.blog import initialize_blog
from service.blog import router as blog_router
from service.threads import list_user_threads
from service.utils import (
    convert_message_content_to_string,
    ensure_model_available,
    langchain_to_chat_message,
    messages_from_checkpoint,
    remove_tool_calls,
)
from tenancy import registration as _registration  # noqa: F401
from tenancy.api import (
    TenantMiddleware,
    business_store,
    current_principal,
)
from tenancy.api import (
    router as tenant_router,
)
from tenancy.security import public_settings
from tenancy.store import tenant_database

warnings.filterwarnings("ignore", category=LangChainBetaWarning)
logger = logging.getLogger(__name__)


def custom_generate_unique_id(route: APIRoute) -> str:
    """Generate idiomatic operation IDs for OpenAPI client generation."""
    return route.name


def verify_bearer(
    http_auth: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(HTTPBearer(description="Please provide AUTH_SECRET api key.", auto_error=False)),
    ],
) -> None:
    if settings.TENANT_MODE:
        if not current_principal():
            raise HTTPException(status_code=401)
        return
    if not settings.AUTH_SECRET:
        return
    auth_secret = settings.AUTH_SECRET.get_secret_value()
    if not http_auth or http_auth.credentials != auth_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Configurable lifespan that initializes the appropriate database checkpointer, store,
    and agents with async loading - for example for starting up MCP clients.
    """
    # FastAPI lifespan + 异步上下文管理器：在接收请求前打开存储，服务退出时释放资源。
    try:
        public_settings().validate_production(settings)
        if not settings.TENANT_MODE:
            await asyncio.to_thread(initialize_blog)
        # Initialize both checkpointer (for short-term memory) and store (for long-term memory)
        async with (
            tenant_database(app),
            initialize_database() as saver,
            initialize_store() as store,
        ):
            # Set up both components
            if hasattr(saver, "setup"):  # ignore: union-attr
                await saver.setup()
            # Only setup store for Postgres as InMemoryStore doesn't need setup
            if hasattr(store, "setup"):  # ignore: union-attr
                await store.setup()

            if not settings.AUTH_SECRET and not settings.TENANT_MODE:
                logger.warning(
                    "AUTH_SECRET is not configured — all API endpoints are unauthenticated. "
                    "Set AUTH_SECRET in your environment to enable bearer token authentication."
                )

            # Configure agents with both memory components and async loading
            agents = get_all_agent_info()
            if settings.TENANT_MODE:
                agents = [a for a in agents if a.key == "dev-issue-agent"]
            for a in agents:
                try:
                    await load_agent(a.key)
                    logger.info(f"Agent loaded: {a.key}")
                except Exception as e:
                    logger.error(f"Failed to load agent {a.key}: {e}")
                    # Continue with other agents rather than failing startup

                agent = get_agent(a.key)
                # Set checkpointer for thread-scoped memory (conversation history)
                agent.checkpointer = saver
                # Set store for long-term memory (cross-conversation knowledge)
                agent.store = store
            get_tracing_client(settings)
            try:
                yield
            finally:
                await asyncio.to_thread(shutdown_tracing)
    except Exception as e:
        logger.error(f"Error during database/store/agents initialization: {e}")
        raise


app = FastAPI(lifespan=lifespan, generate_unique_id_function=custom_generate_unique_id)
app.add_middleware(TenantMiddleware)
app.include_router(tenant_router)
if settings.METRICS_ENABLED:
    from observability.http import install_metrics

    install_metrics(app)
# FastAPI Depends：为本 router 的接口复用 Bearer 校验；未配置 AUTH_SECRET 时直接放行。
router = APIRouter(dependencies=[Depends(verify_bearer)])
# AG-UI protocol endpoints inherit the same bearer auth - see service/agui.py
if not public_settings().PUBLIC_DEMO:
    from service.agui import router as agui_router

    router.include_router(agui_router)
router.include_router(blog_router)


@router.get("/info")
async def info() -> ServiceMetadata:
    models = list(settings.AVAILABLE_MODELS)
    models.sort()
    return ServiceMetadata(
        agents=[
            a
            for a in get_all_agent_info()
            if not settings.TENANT_MODE or a.key == "dev-issue-agent"
        ],
        models=models,
        default_agent="dev-issue-agent" if settings.TENANT_MODE else DEFAULT_AGENT,
        default_model=settings.DEFAULT_MODEL,
    )


async def _handle_input(
    user_input: UserInput, agent: AgentGraph, agent_id: str
) -> tuple[dict[str, Any], UUID]:
    """
    Parse user input and handle any required interrupt resumption.
    Returns kwargs for agent invocation and the run_id.
    """
    run_id = uuid7()
    thread_id = user_input.thread_id or str(uuid4())
    user_id = user_input.user_id or str(uuid4())

    principal = current_principal()
    external_thread_id = thread_id
    if principal:
        policy = public_settings()
        if policy.PUBLIC_DEMO:
            if not policy.PAID_CALLS_ENABLED:
                raise HTTPException(503, "模型服务暂停")
            if len(user_input.message) > 4000:
                raise HTTPException(422, "提问不能超过4000字符")
            if user_input.model is not None and user_input.model != settings.DEFAULT_MODEL:
                raise HTTPException(403, "演示环境仅允许指定模型")
        if user_input.agent_config:
            raise HTTPException(422, "团队工作台不接受自定义 agent_config")
        if agent_id != "dev-issue-agent":
            raise HTTPException(404, "该 Agent 尚未支持多租户")
        user_id = principal.memory_user_id
        thread_id = await business_store().thread(
            principal, external_thread_id, agent_id, title=user_input.message
        )

    configurable: dict[str, Any] = {"thread_id": thread_id, "user_id": user_id}
    if principal:
        configurable["tenant_id"] = principal.tenant_id
        configurable["rag_document_ids"] = [
            row["document_id"] for row in await business_store().documents(principal)
        ]
    if user_input.model is not None:
        ensure_model_available(user_input.model)
        configurable["model"] = user_input.model

    trace_id = trace_id_for(user_input.task_id, run_id)
    callbacks: list[Any] = []
    if handler := tracing_callback(settings, trace_id):
        callbacks.append(handler)

    if user_input.agent_config:
        # Check for reserved keys (including 'model' even if not in configurable)
        reserved_keys = {
            "thread_id",
            "user_id",
            "tenant_id",
            "checkpoint_id",
            "checkpoint_ns",
            "model",
            "task_id",
            "trace_id",
            "run_id",
        }
        overlap = reserved_keys & user_input.agent_config.keys()
        overlap |= {key for key in user_input.agent_config if key.startswith("langfuse_")}
        if overlap:
            raise HTTPException(
                status_code=422,
                detail=f"agent_config contains reserved keys: {overlap}",
            )
        configurable.update(user_input.agent_config)

    # LangChain RunnableConfig：把模型选项和会话标识传给图；thread_id 用于查找 checkpoint。
    config = RunnableConfig(
        configurable=configurable,
        metadata=trace_metadata(agent_id, thread_id, user_id, run_id, trace_id, user_input.task_id),
        run_name=f"supportpilot/{agent_id}",
        run_id=run_id,
        callbacks=callbacks,
    )

    # Check for interrupts that need to be resumed
    if principal:
        config["metadata"]["tenant_id"] = principal.tenant_id
        await business_store().start_run(principal, run_id, external_thread_id, user_input.message)
    try:
        state = await agent.aget_state(config=config)
    except BaseException as exc:
        if principal:
            with CancelScope(shield=True):
                await business_store().finish_run(
                    principal,
                    run_id,
                    "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                )
        raise

    interrupted_tasks = [
        task for task in state.tasks if hasattr(task, "interrupts") and task.interrupts
    ]

    input: Command | dict[str, Any]
    if interrupted_tasks:
        # assume user input is response to resume agent execution from interrupt
        input = Command(resume=user_input.message)
    else:
        input = {"messages": [HumanMessage(content=user_input.message)]}

    kwargs = {
        "input": input,
        "config": config,
    }

    return kwargs, run_id


@router.post("/{agent_id}/invoke", operation_id="invoke_with_agent_id")
@router.post("/invoke")
async def invoke(user_input: UserInput, agent_id: str = DEFAULT_AGENT) -> ChatMessage:
    """
    Invoke an agent with user input to retrieve a final response.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.
    """
    # NOTE: Currently this only returns the last message or interrupt.
    # In the case of an agent outputting multiple AIMessages (such as the background step
    # in interrupt-agent, or a tool step in research-assistant), it's omitted. Arguably,
    # you'd want to include it. You could update the API to return a list of ChatMessages
    # in that case.
    if current_principal() and agent_id == DEFAULT_AGENT:
        agent_id = "dev-issue-agent"
    agent: AgentGraph = get_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, agent_id)

    try:
        response_events: list[tuple[str, Any]] = await agent.ainvoke(**kwargs, stream_mode=["updates", "values"])  # type: ignore # fmt: skip
        response_type, response = response_events[-1]
        # A run that stops on an interrupt reports it on the final event of either stream
        # mode, so check for the interrupt before falling back to the last message.
        if "__interrupt__" in response:
            # Return the value of the first interrupt as an AIMessage
            output = langchain_to_chat_message(
                AIMessage(content=response["__interrupt__"][0].value)
            )
        elif response_type == "values":
            # Normal response, the agent completed successfully
            output = langchain_to_chat_message(response["messages"][-1])
        else:
            raise ValueError(f"Unexpected response type: {response_type}")

        output.run_id = str(run_id)
        if principal := current_principal():
            evidence = [
                langchain_to_chat_message(m).model_dump()
                for m in response.get("messages", [])
                if isinstance(m, ToolMessage)
            ]
            await business_store().finish_run(
                principal, run_id, "succeeded", output.content, evidence
            )
        return output
    except asyncio.CancelledError:
        if principal := current_principal():
            with CancelScope(shield=True):
                await business_store().finish_run(principal, run_id, "interrupted")
        raise
    except Exception as e:
        if principal := current_principal():
            await business_store().finish_run(principal, run_id, "failed")
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")


async def message_generator(
    user_input: StreamInput,
    agent_id: str = DEFAULT_AGENT,
    prepared: tuple[dict[str, Any], UUID] | None = None,
) -> AsyncGenerator[str, None]:
    """
    Generate a stream of messages from the agent.

    This is the workhorse method for the /stream endpoint.
    """
    agent: AgentGraph = get_agent(agent_id)
    kwargs, run_id = prepared or await _handle_input(user_input, agent, agent_id)
    principal = current_principal()
    answer = ""
    evidence = []
    outcome = "interrupted"

    try:
        # Process streamed events from the graph and yield messages over the SSE stream.
        async for stream_event in agent.astream(  # type: ignore[no-matching-overload]
            **kwargs, stream_mode=["updates", "messages", "custom"], subgraphs=True
        ):
            if not isinstance(stream_event, tuple):
                continue
            # Handle different stream event structures based on subgraphs
            if len(stream_event) == 3:
                # With subgraphs=True: (node_path, stream_mode, event)
                _, stream_mode, event = stream_event
            else:
                # Without subgraphs: (stream_mode, event)
                stream_mode, event = stream_event
            new_messages: list[Any] = []
            if stream_mode == "updates":
                for node, updates in event.items():
                    # A simple approach to handle agent interrupts.
                    # In a more sophisticated implementation, we could add
                    # some structured ChatMessage type to return the interrupt value.
                    if node == "__interrupt__":
                        interrupt: Interrupt
                        for interrupt in updates:
                            new_messages.append(AIMessage(content=interrupt.value))
                        continue
                    updates = updates or {}
                    update_messages = updates.get("messages", [])
                    # special cases for using langgraph-supervisor library
                    if "supervisor" in node or "sub-agent" in node:
                        # the only tools that come from the actual agent are the handoff and handback tools
                        if isinstance(update_messages[-1], ToolMessage):
                            if "sub-agent" in node and len(update_messages) > 1:
                                # If this is a sub-agent, we want to keep the last 2 messages - the handback tool, and it's result
                                update_messages = update_messages[-2:]
                            else:
                                # If this is a supervisor, we want to keep the last message only - the handoff result. The tool comes from the 'agent' node.
                                update_messages = [update_messages[-1]]
                        else:
                            update_messages = []
                    new_messages.extend(update_messages)

            if stream_mode == "custom":
                new_messages = [event]

            # LangGraph streaming may emit tuples: (field_name, field_value)
            # e.g. ('content', <str>), ('tool_calls', [ToolCall,...]), ('additional_kwargs', {...}), etc.
            # We accumulate only supported fields into `parts` and skip unsupported metadata.
            # More info at: https://langchain-ai.github.io/langgraph/cloud/how-tos/stream_messages/
            processed_messages = []
            current_message: dict[str, Any] = {}
            for message in new_messages:
                if isinstance(message, tuple):
                    key, value = message
                    # Store parts in temporary dict
                    current_message[key] = value
                else:
                    # Add complete message if we have one in progress
                    if current_message:
                        processed_messages.append(_create_ai_message(current_message))
                        current_message = {}
                    processed_messages.append(message)

            # Add any remaining message parts
            if current_message:
                processed_messages.append(_create_ai_message(current_message))

            for message in processed_messages:
                try:
                    chat_message = langchain_to_chat_message(message)
                    chat_message.run_id = str(run_id)
                except Exception as e:
                    logger.error(f"Error parsing message: {e}")
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Unexpected error'})}\n\n"
                    continue
                # LangGraph re-sends the input message, which feels weird, so drop it
                if chat_message.type == "human" and chat_message.content == user_input.message:
                    continue
                if chat_message.type == "ai" and not chat_message.tool_calls:
                    answer = chat_message.content
                elif chat_message.type == "tool":
                    evidence.append(chat_message.model_dump())
                yield f"data: {json.dumps({'type': 'message', 'content': chat_message.model_dump()})}\n\n"

            if stream_mode == "messages":
                if not user_input.stream_tokens:
                    continue
                msg, metadata = event
                if "skip_stream" in metadata.get("tags", []):
                    continue
                # For some reason, astream("messages") causes non-LLM nodes to send extra messages.
                # Drop them.
                if not isinstance(msg, AIMessageChunk):
                    continue
                content = remove_tool_calls(msg.content)
                if content:
                    # Empty content in the context of OpenAI usually means
                    # that the model is asking for a tool to be invoked.
                    # So we only print non-empty content.
                    yield f"data: {json.dumps({'type': 'token', 'content': convert_message_content_to_string(content)})}\n\n"
        outcome = "succeeded" if answer else "failed"
    except Exception as e:
        outcome = "failed"
        logger.error(f"Error in message generator: {e}")
        yield f"data: {json.dumps({'type': 'error', 'content': 'Internal server error'})}\n\n"
    finally:
        if principal:
            with CancelScope(shield=True):
                await business_store().finish_run(principal, run_id, outcome, answer, evidence)
        if outcome != "interrupted":
            yield "data: [DONE]\n\n"


def _create_ai_message(parts: dict) -> AIMessage:
    sig = inspect.signature(AIMessage)
    valid_keys = set(sig.parameters)
    filtered = {k: v for k, v in parts.items() if k in valid_keys}
    return AIMessage(**filtered)


def _sse_response_example() -> dict[int | str, Any]:
    return {
        status.HTTP_200_OK: {
            "description": "Server Sent Event Response",
            "content": {
                "text/event-stream": {
                    "example": "data: {'type': 'token', 'content': 'Hello'}\n\ndata: {'type': 'token', 'content': ' World'}\n\ndata: [DONE]\n\n",
                    "schema": {"type": "string"},
                }
            },
        }
    }


@router.post(
    "/{agent_id}/stream",
    response_class=StreamingResponse,
    responses=_sse_response_example(),
    operation_id="stream_with_agent_id",
)
@router.post("/stream", response_class=StreamingResponse, responses=_sse_response_example())
async def stream(user_input: StreamInput, agent_id: str = DEFAULT_AGENT) -> StreamingResponse:
    """
    Stream an agent's response to a user input, including intermediate messages and tokens.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to all messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.

    Set `stream_tokens=false` to return intermediate messages but not token-by-token.
    """
    # SSE（text/event-stream）：在同一个 HTTP 响应中持续发送事件，客户端逐行解析。
    # Tenant authorization and conflict detection must happen before SSE headers.
    prepared = None
    if current_principal():
        if agent_id == DEFAULT_AGENT:
            agent_id = "dev-issue-agent"
        prepared = await _handle_input(user_input, get_agent(agent_id), agent_id)
    return StreamingResponse(
        message_generator(user_input, agent_id, prepared)
        if prepared
        else message_generator(user_input, agent_id),
        media_type="text/event-stream",
    )


@router.post("/feedback")
async def feedback(feedback: Feedback) -> FeedbackResponse:
    """
    Record feedback for a run to LangSmith.

    This is a simple wrapper for the LangSmith create_feedback API, so the
    credentials can be stored and managed in the service rather than the client.
    See: https://api.smith.langchain.com/redoc#tag/feedback/operation/create_feedback_api_v1_feedback_post
    """
    client = LangsmithClient()
    kwargs = feedback.kwargs or {}
    client.create_feedback(
        run_id=feedback.run_id,
        key=feedback.key,
        score=feedback.score,
        **kwargs,
    )
    return FeedbackResponse()


@router.post("/{agent_id}/history", operation_id="history_with_agent_id")
@router.post("/history")
async def history(input: ChatHistoryInput, agent_id: str = DEFAULT_AGENT) -> ChatHistory:
    """
    Get chat history for a thread and agent.

    If agent_id is not provided, the default agent will be used.
    """
    if current_principal() and agent_id == DEFAULT_AGENT:
        agent_id = "dev-issue-agent"
    agent: AgentGraph = get_agent(agent_id)
    thread_id = input.thread_id
    if principal := current_principal():
        thread_id = await business_store().thread(principal, thread_id, agent_id)
    config = RunnableConfig(configurable={"thread_id": thread_id})
    try:
        messages: list[BaseMessage] = []
        # Functional-API agents keep the conversation in `__previous__`, which aget_state
        # doesn't return, so read the raw checkpoint first and only fall back for graphs.
        checkpointer = getattr(agent, "checkpointer", None)
        if checkpointer:
            tup = await checkpointer.aget_tuple(config)
            if tup and "__previous__" in (tup.checkpoint.get("channel_values") or {}):
                messages = messages_from_checkpoint(tup.checkpoint)
        if not messages:
            state_snapshot = await agent.aget_state(config=config)
            messages = state_snapshot.values["messages"]
        chat_messages: list[ChatMessage] = [langchain_to_chat_message(m) for m in messages]
        return ChatHistory(messages=chat_messages)
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")


@router.get("/{agent_id}/threads", operation_id="threads_with_agent_id")
@router.get("/threads")
async def threads(
    input: UserThreadsInput = Depends(), agent_id: str = DEFAULT_AGENT
) -> UserThreads:
    """
    List a user's conversation threads for an agent, most recently updated first.

    `user_id` is asserted by the caller and not checked against the credentials on the
    request, so any holder of the bearer token can list any user's threads - the same
    trust model as /history. Put your own authorization in front of this before end
    users can reach it.
    """
    if principal := current_principal():
        if agent_id == DEFAULT_AGENT:
            agent_id = "dev-issue-agent"
        return UserThreads(threads=await business_store().threads(principal, agent_id, input.limit))
    if input.user_id is None:
        raise HTTPException(422, "本地模式需要 user_id")
    agent: AgentGraph = get_agent(agent_id)
    checkpointer = getattr(agent, "checkpointer", None)
    if not checkpointer:
        return UserThreads(threads=[])

    try:
        summaries = await list_user_threads(checkpointer, input.user_id, agent_id, input.limit)
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")

    return UserThreads(threads=summaries)


@app.get("/health")
async def health_check():
    """Health check endpoint."""

    health_status = {"status": "ok"}

    # Liveness must not synchronously call a third-party service.
    health_status["langfuse"] = tracing_status(settings)

    return health_status


app.include_router(router)
