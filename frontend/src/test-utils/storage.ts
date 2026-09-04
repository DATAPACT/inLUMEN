export const installTestStorage = () => {
  for (const name of ['localStorage', 'sessionStorage']) {
    const values = new Map<string, string>();
    const storage: Storage = {
      get length() { return values.size; },
      clear: () => values.clear(),
      key: index => Array.from(values.keys())[index] ?? null,
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => { values.set(key, value); },
      removeItem: key => { values.delete(key); },
    };
    Object.defineProperty(window, name, { configurable: true, value: storage });
  }
};
