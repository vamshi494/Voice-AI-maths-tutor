// frontend/src/main.tsx
import React from 'react';
import ReactDOM from 'react-dom/client';
import Root from './Root';
import './theme.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
