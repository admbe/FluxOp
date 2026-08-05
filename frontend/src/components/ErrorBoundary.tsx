import { Component, type ErrorInfo, type ReactNode } from "react";

type CaughtError = {
  message: string;
  stack: string;
  componentStack: string;
};

function describe(error: unknown): { message: string; stack: string } {
  return {
    message:
      error instanceof Error ? error.message : String(error ?? "Unknown error"),
    stack: error instanceof Error ? String(error.stack ?? "") : "",
  };
}

/**
 * Contains a render error to one region instead of blanking the application.
 *
 * React unmounts the whole tree when a render throws and nothing catches it,
 * which presents as a white screen with no way to tell what failed. This
 * renders the error text so it can be reported, keeps the rest of the shell
 * usable, and offers a local retry.
 *
 * When a render throws, React unmounts the failed subtree, and cleanup code
 * running during that unmount can throw again into the same boundary. The
 * first error is the cause and later ones are usually fallout, so the first
 * is kept for display and the rest only counted. Every error is also posted
 * to the API with React's component stack, because the browser stack of a
 * production error names minified React internals while the component stack
 * names the component that actually crashed.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; area: string },
  { lastError: { message: string; stack: string } | null; laterErrors: number }
> {
  state: {
    lastError: { message: string; stack: string } | null;
    laterErrors: number;
  } = { lastError: null, laterErrors: 0 };

  // The first caught error, kept outside state because
  // getDerivedStateFromError is static and cannot see previous state.
  private firstError: CaughtError | null = null;

  static getDerivedStateFromError(error: unknown) {
    return { lastError: describe(error) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    const { message, stack } = describe(error);
    const componentStack = String(info.componentStack ?? "");
    console.error(`Flux render error in ${this.props.area}:`, error, info);
    if (this.firstError) {
      this.setState((current) => ({ laterErrors: current.laterErrors + 1 }));
    } else {
      this.firstError = { message, stack, componentStack };
      // Re-render so the fallback picks up the component stack.
      this.setState({ laterErrors: 0 });
    }
    fetch("/api/client-error", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({
        area: this.props.area,
        message: message.slice(0, 2000),
        stack: stack.slice(0, 8000),
        componentStack: componentStack.slice(0, 8000),
        url: window.location.href.slice(0, 500),
      }),
    }).catch(() => undefined);
  }

  render() {
    if (!this.state.lastError && !this.firstError) return this.props.children;
    const shown = this.firstError ?? {
      ...this.state.lastError!,
      componentStack: "",
    };
    return (
      <div className="render-error" role="alert">
        <strong>{this.props.area} could not be displayed.</strong>
        <p>
          The rest of Flux is still usable. This has been reported
          automatically; a reload usually restores the view.
        </p>
        <code>{shown.message}</code>
        {this.state.laterErrors > 0 && (
          <p>
            {this.state.laterErrors} follow-on error
            {this.state.laterErrors === 1 ? "" : "s"} occurred while closing
            the failed view.
          </p>
        )}
        {(shown.stack || shown.componentStack) && (
          <details>
            <summary>Technical detail</summary>
            {shown.componentStack && (
              <pre>In components:{shown.componentStack.slice(0, 1200)}</pre>
            )}
            {shown.stack && <pre>{shown.stack.slice(0, 1200)}</pre>}
          </details>
        )}
        <button
          className="button button--secondary"
          onClick={() => {
            this.firstError = null;
            this.setState({ lastError: null, laterErrors: 0 });
          }}
        >
          Try again
        </button>
      </div>
    );
  }
}
