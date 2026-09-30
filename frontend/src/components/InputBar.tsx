// frontend/src/components/InputBar.tsx
// Input bar and controls

import React, { useEffect, useState } from 'react';
import { Camera, Play, Pause, FastForward, Send, LogOut, Mic, MicOff } from 'lucide-react';

interface InputBarProps {
  isLocked: boolean; // Locked until first step of turn
  hasMarks: boolean;
  canContinueLesson: boolean;
  currentSpeed: number; // 0.8, 1.0, 1.2
  onSendMessage: (text: string, intent?: 'new' | 'auto' | 'topic') => void;
  onExplainMarks: () => void;
  onTogglePause: () => void;
  isPaused: boolean;
  onChangeSpeed: (speed: number) => void;
  onContinueLesson: () => void;
  onEndClass: () => void;
  onOpenPhotoModal: () => void;
  /** Student microphone (LiveKit setMicrophoneEnabled). */
  isMicMuted: boolean;
  onToggleMic: () => void;
  /** Photo -> text draft to confirm or edit before asking. */
  draftText?: string | null;
  onDraftConsumed?: () => void;
}

export const InputBar: React.FC<InputBarProps> = ({
  isLocked,
  hasMarks,
  canContinueLesson,
  currentSpeed,
  onSendMessage,
  onExplainMarks,
  onTogglePause,
  isPaused,
  onChangeSpeed,
  onContinueLesson,
  onEndClass,
  onOpenPhotoModal,
  isMicMuted,
  onToggleMic,
  draftText,
  onDraftConsumed,
}) => {
  const [inputText, setInputText] = useState('');

  useEffect(() => {
    if (draftText) {
      setInputText(draftText);
      onDraftConsumed?.();
    }
  }, [draftText, onDraftConsumed]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleSubmit = () => {
    const text = inputText.trim();
    if (text) {
      // With marks present, App routes this to submit_doubt WITH the typed text;
      // onExplainMarks() alone would discard what the student typed.
      onSendMessage(text);
      setInputText('');
    } else if (hasMarks) {
      onExplainMarks();
    }
  };

  /** An explicit topic request starts an outlined multi-page chapter. */
  const handleTeachTopic = () => {
    const text = inputText.trim();
    if (!text) return;
    onSendMessage(text, 'topic');
    setInputText('');
  };

  const cycleSpeed = () => {
    if (currentSpeed === 0.8) onChangeSpeed(1.0);
    else if (currentSpeed === 1.0) onChangeSpeed(1.2);
    else onChangeSpeed(0.8);
  };

  return (
    <div
      className="glass-panel"
      style={{
        margin: '0 24px 20px 24px',
        padding: '10px 18px',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        zIndex: 50,
      }}
    >
      {/* Photo capture button */}
      <button
        onClick={onOpenPhotoModal}
        title="Upload photo question"
        disabled={isLocked}
        style={{
          background: 'transparent',
          border: 'none',
          color: '#cbd5e1',
          cursor: isLocked ? 'not-allowed' : 'pointer',
          padding: 6,
          display: 'flex',
          alignItems: 'center',
          opacity: isLocked ? 0.4 : 1,
        }}
      >
        <Camera size={20} />
      </button>

      {/* Input box */}
      <input
        type="text"
        placeholder={
          isLocked
            ? 'Teacher Vamshi is writing on the board...'
            : hasMarks
              ? 'Type an optional doubt or click Explain this...'
              : 'Ask a math question (e.g. Find roots of x^2 - 5x + 6 = 0)...'
        }
        value={inputText}
        onChange={(e) => setInputText(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={isLocked}
        style={{
          flex: 1,
          background: 'rgba(0, 0, 0, 0.25)',
          border: '1px solid rgba(255, 255, 255, 0.1)',
          borderRadius: 8,
          padding: '10px 14px',
          color: '#fef3c7',
          fontSize: 14,
          outline: 'none',
          opacity: isLocked ? 0.5 : 1,
        }}
      />

      {/* Teach a topic: send the typed text as an outlined chapter request */}
      <button
        onClick={handleTeachTopic}
        disabled={isLocked || !inputText.trim()}
        title="Teach a topic as a multi-page chapter"
        style={{
          backgroundColor: 'rgba(56, 189, 248, 0.15)',
          border: '1px solid rgba(56, 189, 248, 0.4)',
          color: '#7dd3fc',
          fontWeight: 600,
          fontSize: 12,
          borderRadius: 8,
          padding: '10px 12px',
          cursor: isLocked || !inputText.trim() ? 'not-allowed' : 'pointer',
          opacity: isLocked || !inputText.trim() ? 0.4 : 1,
          whiteSpace: 'nowrap',
        }}
      >
        Teach a topic
      </button>

      {/* Submit button: "Ask" vs "Explain this" */}
      <button
        onClick={handleSubmit}
        disabled={isLocked || (!hasMarks && !inputText.trim())}
        style={{
          backgroundColor: hasMarks ? '#f43f5e' : '#f59e0b',
          color: hasMarks ? '#fff' : '#000',
          fontWeight: 600,
          fontSize: 13,
          border: 'none',
          borderRadius: 8,
          padding: '10px 18px',
          cursor: isLocked || (!hasMarks && !inputText.trim()) ? 'not-allowed' : 'pointer',
          opacity: isLocked || (!hasMarks && !inputText.trim()) ? 0.4 : 1,
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          transition: 'all 0.2s ease',
        }}
      >
        <span>{hasMarks ? 'Explain this' : 'Ask'}</span>
        <Send size={15} />
      </button>

      {/* Divider */}
      <div style={{ width: 1, height: 24, background: 'rgba(255, 255, 255, 0.15)' }} />

      {/* Microphone mute / unmute */}
      <button
        onClick={onToggleMic}
        title={isMicMuted ? 'Unmute microphone' : 'Mute microphone'}
        aria-label={isMicMuted ? 'Unmute microphone' : 'Mute microphone'}
        aria-pressed={isMicMuted}
        style={{
          background: isMicMuted ? 'rgba(239, 68, 68, 0.15)' : 'transparent',
          border: 'none',
          borderRadius: 6,
          color: isMicMuted ? '#f87171' : '#cbd5e1',
          cursor: 'pointer',
          padding: 6,
          display: 'flex',
          alignItems: 'center',
        }}
      >
        {isMicMuted ? <MicOff size={18} /> : <Mic size={18} />}
      </button>

      {/* Pause / Resume button */}
      <button
        onClick={onTogglePause}
        title={isPaused ? 'Resume the tutor' : 'Pause the tutor (pick up the marker)'}
        style={{
          background: 'transparent',
          border: 'none',
          color: '#cbd5e1',
          cursor: 'pointer',
          padding: 6,
        }}
      >
        {isPaused ? <Play size={18} color="#10b981" /> : <Pause size={18} />}
      </button>

      {/* Speed control */}
      <button
        onClick={cycleSpeed}
        title="Speech Speed"
        style={{
          background: 'rgba(255, 255, 255, 0.05)',
          border: '1px solid rgba(255, 255, 255, 0.1)',
          borderRadius: 6,
          color: '#cbd5e1',
          fontSize: 12,
          fontWeight: 600,
          cursor: 'pointer',
          padding: '4px 8px',
          display: 'flex',
          alignItems: 'center',
          gap: 4,
        }}
      >
        <FastForward size={13} />
        {currentSpeed}x
      </button>

      {/* Continue lesson (visible only when canContinueLesson is true) */}
      {canContinueLesson && (
        <button
          onClick={onContinueLesson}
          style={{
            backgroundColor: '#10b981',
            color: '#fff',
            fontWeight: 600,
            fontSize: 12,
            border: 'none',
            borderRadius: 6,
            padding: '6px 12px',
            cursor: 'pointer',
          }}
        >
          Continue lesson
        </button>
      )}

      {/* End class */}
      <button
        onClick={onEndClass}
        title="End Class"
        style={{
          background: 'transparent',
          border: 'none',
          color: '#ef4444',
          cursor: 'pointer',
          padding: 6,
        }}
      >
        <LogOut size={18} />
      </button>
    </div>
  );
};
