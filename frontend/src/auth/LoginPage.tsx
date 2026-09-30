// frontend/src/auth/LoginPage.tsx
import React, { useState } from 'react';
import { cleanName, NAME_MAX_LEN } from './session';

interface Props {
  onLogin(name: string): void;
}

export const LoginPage: React.FC<Props> = ({ onLogin }) => {
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | null>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const name = cleanName(value);
    if (!name) {
      setError('Please enter your name.');
      return;
    }
    onLogin(name);
  };

  return (
    <div
      style={{
        minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'var(--bg-app)', padding: 16, fontFamily: 'var(--font-ui)',
      }}
    >
      <form
        onSubmit={submit}
        aria-label="Sign in"
        style={{
          width: '100%', maxWidth: 380, background: 'var(--bg-board)', padding: '32px 28px',
          borderRadius: 14, border: '1px solid var(--panel-border)', boxShadow: 'var(--panel-shadow)',
          display: 'flex', flexDirection: 'column', gap: 16,
        }}
      >
        <div>
          <h1 style={{ fontFamily: 'var(--font-display)', color: 'var(--chalk-text)', fontSize: 24 }}>
            AI Math Tutor
          </h1>
          <p style={{ color: 'var(--chalk-dim)', fontSize: 14, marginTop: 6 }}>
            Enter your name to start. Use the same name next time to pick up where you left off.
          </p>
        </div>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--chalk-dim)', fontSize: 13 }}>
          Your name
          <input
            autoFocus
            value={value}
            maxLength={NAME_MAX_LEN}
            onChange={(e) => { setValue(e.target.value); setError(null); }}
            placeholder="e.g. Priya"
            style={{
              padding: '10px 12px', borderRadius: 8, fontSize: 16, color: 'var(--chalk-text)',
              background: 'var(--bg-board-frame)', border: '1px solid var(--panel-border)', outline: 'none',
            }}
          />
        </label>
        {error && <div role="alert" style={{ color: 'var(--chalk-rose)', fontSize: 13 }}>{error}</div>}
        <button
          type="submit"
          style={{
            padding: '10px 14px', borderRadius: 8, border: 'none', fontSize: 15, fontWeight: 600,
            background: 'var(--accent-amber)', color: '#1a1206', cursor: 'pointer',
          }}
        >
          Start learning
        </button>
      </form>
    </div>
  );
};
