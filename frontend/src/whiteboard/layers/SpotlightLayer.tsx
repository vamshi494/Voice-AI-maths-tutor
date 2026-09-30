// frontend/src/whiteboard/layers/SpotlightLayer.tsx
import React from 'react';
import { Layer } from 'react-konva';
import Konva from 'konva';

interface Props {
  layerRef?: React.Ref<Konva.Layer>;
}

export const SpotlightLayer: React.FC<Props> = ({ layerRef }) => {
  return <Layer ref={layerRef} listening={false} perfectDrawEnabled={true} name="spotlightLayer" />;
};
