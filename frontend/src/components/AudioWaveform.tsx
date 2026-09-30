// frontend/src/components/AudioWaveform.tsx
import React from 'react';

interface Props {
  isActive: boolean;
  isSpeaking: boolean;
  tutorName?: string;
}

export const AudioWaveform: React.FC<Props> = ({ isActive, isSpeaking, tutorName = 'Vamshi' }) => {
  return (
    <div
      className="glass-panel"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '6px 14px',
        borderRadius: 20,
      }}
    >
      <div
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          backgroundColor: isActive ? '#10b981' : '#64748b',
          boxShadow: isActive ? '0 0 8px #10b981' : 'none',
        }}
      />
      <span style={{ fontSize: 13, fontWeight: 600, color: '#fef3c7' }}>{tutorName}</span>

      {/* Pulsing bars when tutor is speaking */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 3, height: 16 }}>
        {[1, 2, 3, 4].map((bar) => (
          <div
            key={bar}
            style={{
              width: 3,
              height: isSpeaking ? `${8 + (bar % 3) * 4}px` : '4px',
              backgroundColor: isSpeaking ? '#f59e0b' : '#64748b',
              borderRadius: 2,
              transition: 'height 0.15s ease',
            }}
          />
        ))}
      </div>
    </div>
  );
};
