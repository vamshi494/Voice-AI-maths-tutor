// frontend/src/components/LessonProgress.tsx
// Chapter progress chips from the `lesson_plan` event. Pure display: it
// never touches the matcher, executor or audio — reading it cannot disturb sync.
import React from 'react';
import { BookOpen } from 'lucide-react';
import { PageHeader } from '../types/events';

interface LessonProgressProps {
  pages: PageHeader[];
  currentIndex: number | null;
}

export const LessonProgress: React.FC<LessonProgressProps> = ({ pages, currentIndex }) => {
  if (!pages || pages.length === 0) return null;
  return (
    <div
      data-testid="lesson-progress"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 24px 0 24px',
        overflowX: 'auto',
        zIndex: 20,
      }}
    >
      <BookOpen size={14} color="#f59e0b" style={{ flex: '0 0 auto' }} />
      {pages.map((page) => {
        const active = currentIndex !== null && page.index === currentIndex;
        return (
          <span
            key={page.pageId}
            title={page.title}
            style={{
              flex: '0 0 auto',
              fontSize: 12,
              padding: '3px 10px',
              borderRadius: 12,
              border: `1px solid ${active ? 'rgba(245,158,11,0.8)' : 'rgba(255,255,255,0.12)'}`,
              backgroundColor: active ? 'rgba(245,158,11,0.18)' : 'rgba(255,255,255,0.04)',
              color: active ? '#fde68a' : '#94a3b8',
              fontWeight: active ? 600 : 400,
              whiteSpace: 'nowrap',
            }}
          >
            {page.index + 1}. {page.title}
          </span>
        );
      })}
    </div>
  );
};
