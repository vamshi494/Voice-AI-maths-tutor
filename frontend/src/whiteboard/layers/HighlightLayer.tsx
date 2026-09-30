// frontend/src/whiteboard/layers/HighlightLayer.tsx
import React from 'react';
import { Layer } from 'react-konva';
import Konva from 'konva';

interface Props {
  layerRef?: React.Ref<Konva.Layer>;
}

export const HighlightLayer: React.FC<Props> = ({ layerRef }) => {
  return <Layer ref={layerRef} listening={false} perfectDrawEnabled={false} name="highlightLayer" />;
};
