export const VOICE_END_SILENCE_MS = 1_000

export function hasVoiceEndSilenceElapsed(quietSince: number | null, now: number): boolean {
  return quietSince !== null && now - quietSince >= VOICE_END_SILENCE_MS
}
