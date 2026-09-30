// frontend/src/__tests__/transcriptSource.test.ts
import { describe, expect, it } from 'vitest';
import { isTutorTranscriptSender } from '../app/transcriptSource';

describe('only the tutor transcript feeds the word matcher', () => {
  const agent = { identity: 'agent-AJ_x', isAgent: true };

  it('student_own_stt_is_rejected', () => {
    // sender = the local student; remoteParticipants.get(local) is undefined
    expect(isTutorTranscriptSender('student-42', 'student-42', undefined)).toBe(false);
  });

  it('agent_transcript_is_accepted', () => {
    expect(isTutorTranscriptSender('agent-AJ_x', 'student-42', agent)).toBe(true);
  });

  it('other_humans_are_rejected', () => {
    expect(isTutorTranscriptSender('teacher-observer', 'student-42',
      { identity: 'teacher-observer', isAgent: false })).toBe(false);
  });

  it('unknown_sender_is_rejected', () => {
    expect(isTutorTranscriptSender('ghost', 'student-42', undefined)).toBe(false);
  });
});
