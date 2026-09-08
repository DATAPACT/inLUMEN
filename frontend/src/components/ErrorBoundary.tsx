import { Component, type ErrorInfo, type ReactNode } from 'react';

export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: Error, _info: ErrorInfo) {
    console.error('Editor rendering failed', error.name);
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return <main className="mx-auto max-w-lg p-8" role="alert">
      <h1 className="text-xl font-semibold">The editor could not be displayed</h1>
      <p className="my-4">Your saved pipeline and browser drafts have been kept. Reload the editor to recover.</p>
      <button className="rounded border px-4 py-2" onClick={() => window.location.reload()}>Reload editor</button>
    </main>;
  }
}
