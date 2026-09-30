// frontend/src/components/PhotoUploadModal.tsx
// Photo question — client-side resize to <=1024, JPEG 0.8

import React, { useState, useRef } from 'react';
import { Upload, X, Check, Loader2, AlertCircle } from 'lucide-react';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onUploadImage: (fileBlob: Blob) => Promise<{ success: boolean; message?: string } | void>;
}

export const PhotoUploadModal: React.FC<Props> = ({ isOpen, onClose, onUploadImage }) => {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [processedBlob, setProcessedBlob] = useState<Blob | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  if (!isOpen) return null;

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setErrorMessage(null);

    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement('canvas');
      let width = img.width;
      let height = img.height;

      // Scale so longest edge <= 1024
      const maxDim = 1024;
      if (width > maxDim || height > maxDim) {
        if (width > height) {
          height = Math.round((height * maxDim) / width);
          width = maxDim;
        } else {
          width = Math.round((width * maxDim) / height);
          height = maxDim;
        }
      }

      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext('2d');
      if (ctx) {
        ctx.drawImage(img, 0, 0, width, height);
        canvas.toBlob(
          (blob) => {
            if (blob) {
              setProcessedBlob(blob);
              setPreviewUrl(URL.createObjectURL(blob));
            }
          },
          'image/jpeg',
          0.8
        );
      }
    };
    img.src = URL.createObjectURL(file);
  };

  const handleConfirm = async () => {
    if (!processedBlob || isSubmitting) return;
    setIsSubmitting(true);
    setErrorMessage(null);

    try {
      const result = await onUploadImage(processedBlob);
      if (result && !result.success && result.message) {
        setErrorMessage(result.message);
      } else {
        onClose();
      }
    } catch (err: any) {
      setErrorMessage(err.message || 'Failed to extract question from photo');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.7)',
        backdropFilter: 'blur(8px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 100,
      }}
    >
      <div
        className="glass-panel"
        style={{
          width: 500,
          padding: 24,
          display: 'flex',
          flexDirection: 'column',
          gap: 18,
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ fontSize: 18, fontWeight: 600, color: '#fef3c7' }}>Upload Textbook Problem</h3>
          <button
            onClick={onClose}
            disabled={isSubmitting}
            style={{ background: 'transparent', border: 'none', color: '#cbd5e1', cursor: 'pointer' }}
          >
            <X size={20} />
          </button>
        </div>

        <input
          type="file"
          ref={fileInputRef}
          accept="image/*"
          style={{ display: 'none' }}
          onChange={handleFileSelect}
        />

        {previewUrl ? (
          <div style={{ textAlign: 'center', borderRadius: 8, overflow: 'hidden', maxHeight: 280, position: 'relative' }}>
            <img src={previewUrl} alt="Preview" style={{ maxWidth: '100%', maxHeight: 280, objectFit: 'contain' }} />
          </div>
        ) : (
          <div
            onClick={() => fileInputRef.current?.click()}
            style={{
              border: '2px dashed rgba(255, 255, 255, 0.2)',
              borderRadius: 12,
              padding: 36,
              textAlign: 'center',
              cursor: 'pointer',
            }}
          >
            <Upload size={36} color="#f59e0b" style={{ margin: '0 auto 12px auto' }} />
            <p style={{ color: '#fef3c7', fontSize: 14 }}>Click to take photo or choose image</p>
            <p style={{ color: '#cbd5e1', fontSize: 12, marginTop: 4 }}>Automatically scaled to 1024px JPEG</p>
          </div>
        )}

        {errorMessage && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '8px 12px',
              backgroundColor: 'rgba(239, 68, 68, 0.15)',
              border: '1px solid rgba(239, 68, 68, 0.4)',
              borderRadius: 8,
              color: '#fca5a5',
              fontSize: 13,
            }}
          >
            <AlertCircle size={16} />
            <span>{errorMessage}</span>
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12, marginTop: 4 }}>
          <button
            onClick={onClose}
            disabled={isSubmitting}
            style={{
              background: 'transparent',
              border: '1px solid rgba(255, 255, 255, 0.2)',
              borderRadius: 8,
              color: '#cbd5e1',
              padding: '8px 16px',
              cursor: isSubmitting ? 'not-allowed' : 'pointer',
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            disabled={!processedBlob || isSubmitting}
            style={{
              backgroundColor: '#f59e0b',
              color: '#000',
              fontWeight: 600,
              border: 'none',
              borderRadius: 8,
              padding: '8px 18px',
              cursor: processedBlob && !isSubmitting ? 'pointer' : 'not-allowed',
              opacity: processedBlob && !isSubmitting ? 1 : 0.5,
              display: 'flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            {isSubmitting ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                Extracting Question...
              </>
            ) : (
              <>
                <Check size={16} />
                Submit Photo
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
};
