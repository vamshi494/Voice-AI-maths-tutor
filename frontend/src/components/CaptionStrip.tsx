// frontend/src/components/CaptionStrip.tsx
// The spoken text shown when TTS is unavailable (captions mode).
import React from 'react';

interface CaptionStripProps {
  text: string | null;
}

export const CaptionStrip: React.FC<CaptionStripProps> = ({ text }) => {
  if (!text) return null;
  return (
    <div
      role="status"
      aria-label="captions"
      style={{
        position: 'absolute',
        left: '50%',
        bottom: 96,
        transform: 'translateX(-50%)',
        maxWidth: '80%',
        backgroundColor: 'rgba(15, 23, 42, 0.94)',
        border: '1px solid rgba(148, 163, 184, 0.4)',
        color: '#e2e8f0',
        padding: '10px 20px',
        borderRadius: 12,
        fontSize: 17,
        lineHeight: 1.45,
        zIndex: 36,
        pointerEvents: 'none',
      }}
    >
      {text}
    </div>
  );
};
