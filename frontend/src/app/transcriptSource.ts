// frontend/src/app/transcriptSource.ts
// Which `lk.transcription` streams are the TUTOR's words (the only input of the
// transcript matcher).
//
// livekit-agents publishes BOTH transcripts on the `lk.transcription` topic from the agent's
// connection: its own speech with sender_identity = the agent, and the student's STT with
// sender_identity = THE STUDENT (room_io `_ParticipantTranscriptionOutput(participant=<linked
// user>)`). The student's own identity is never in `room.remoteParticipants`, so a check like
// `from && !isAgent(from)` would let every student transcript (and the tutor's own voice
// echoed back through the laptop mic) into the tutor-word matcher as if the tutor had said it.

export interface SenderLike {
  identity?: string;
  isAgent?: boolean;
}

export function isTutorTranscriptSender(
  senderIdentity: string | undefined,
  localIdentity: string | undefined,
  remote: SenderLike | undefined,
): boolean {
  if (!senderIdentity) return true;                        // legacy stream without sender: keep
  if (localIdentity && senderIdentity === localIdentity) return false;  // the student's own STT
  if (!remote) return false;                               // unknown sender: never the matcher
  return Boolean(remote.isAgent || remote.identity?.includes('agent'));
}
