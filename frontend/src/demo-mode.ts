/** Only the dedicated demo build enables the local, backend-free transport. */
export const STATIC_DEMO = import.meta.env.MODE === 'demo';
