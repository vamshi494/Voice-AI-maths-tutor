// frontend/src/Root.tsx
// Name gate: no name -> login page; a name -> the tutor, remounted per user.
import React, { useState } from 'react';
import App from './App';
import { LoginPage } from './auth/LoginPage';
import { clearName, getStoredName, storeName } from './auth/session';

export const Root: React.FC = () => {
  const [name, setName] = useState<string | null>(() => getStoredName());

  if (!name) {
    return <LoginPage onLogin={(n) => { storeName(n); setName(n); }} />;
  }
  return <App key={name} userName={name} onLogout={() => { clearName(); setName(null); }} />;
};

export default Root;
