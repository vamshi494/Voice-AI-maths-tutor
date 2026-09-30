// frontend/src/whiteboard/layers/BackgroundLayer.tsx
import React from 'react';
import { Layer, Rect, Line } from 'react-konva';

interface BackgroundLayerProps {
  dividers?: number[];
}

export const BackgroundLayer: React.FC<BackgroundLayerProps> = ({ dividers = [395, 785] }) => {
  return (
    <Layer listening={false} perfectDrawEnabled={false}>
      {/* Board background slate chalkboard */}
      <Rect
        x={0}
        y={0}
        width={1200}
        height={700}
        fill="#16201b"
      />
      {/* Subtle column divider dashed lines */}
      {dividers.map((x, idx) => (
        <Line
          key={idx}
          points={[x, 40, x, 660]}
          stroke="rgba(255, 255, 255, 0.08)"
          strokeWidth={1}
          dash={[4, 4]}
        />
      ))}
    </Layer>
  );
};
