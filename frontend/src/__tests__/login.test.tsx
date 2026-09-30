// frontend/src/__tests__/login.test.tsx
// Name login: the login page accepts only a name; the session helpers mirror the server rules.
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { LoginPage } from '../auth/LoginPage';
import {
  cleanName, clearName, getStoredName, requestNewBoard, storeName, takeNewBoardRequest,
} from '../auth/session';

function mount(onLogin: (n: string) => void) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => { root.render(<LoginPage onLogin={onLogin} />); });
  return { host, root };
}

function type(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

afterEach(() => {
  document.body.innerHTML = '';
  clearName();
});

describe('name login', () => {
  it('submits_the_cleaned_name', () => {
    const onLogin = vi.fn();
    const { host } = mount(onLogin);
    type(host.querySelector('input')!, '  Priya   <b>S</b> ');
    act(() => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
    expect(onLogin).toHaveBeenCalledWith('Priya b S b');
  });

  it('rejects_an_empty_name', () => {
    const onLogin = vi.fn();
    const { host } = mount(onLogin);
    type(host.querySelector('input')!, '   ');
    act(() => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
    expect(onLogin).not.toHaveBeenCalled();
    expect(host.querySelector('[role="alert"]')?.textContent).toMatch(/enter your name/i);
  });

  it('stores_and_clears_the_name', () => {
    expect(getStoredName()).toBeNull();
    storeName('Asha');
    expect(getStoredName()).toBe('Asha');
    clearName();
    expect(getStoredName()).toBeNull();
  });

  it('new_board_request_is_one_shot', () => {
    requestNewBoard();
    expect(takeNewBoardRequest()).toBe(true);
    expect(takeNewBoardRequest()).toBe(false);
  });

  it('clean_name_matches_server_rules', () => {
    expect(cleanName("O'Brien-Smith Jr.")).toBe("O'Brien-Smith Jr.");
    expect(cleanName('x'.repeat(60)).length).toBe(40);
  });
});
