// The upstream plugin exports name "review"; the host keys loader entries by id.
// Re-export the unchanged plugin under a distinct registration name/id so the
// quality bundle's @stardustlc/dsh-review (also id "review") keeps its tools.
export { apply, inject } from '@apcamargo/dsh-review';
export const name = 'sextant-review';
