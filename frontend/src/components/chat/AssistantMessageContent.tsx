import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

type AssistantMessageContentProps = {
  content: string;
};

export const AssistantMessageContent = ({ content }: AssistantMessageContentProps) => (
  <div className="min-w-0 break-words text-[13px] leading-6">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }) => (
          <p className="my-2 first:mt-0 last:mb-0">{children}</p>
        ),
        h1: ({ children }) => (
          <h1 className="mb-2 mt-4 text-base font-semibold tracking-tight first:mt-0">
            {children}
          </h1>
        ),
        h2: ({ children }) => (
          <h2 className="mb-1.5 mt-4 border-b border-border/70 pb-1 text-sm font-semibold tracking-tight first:mt-0">
            {children}
          </h2>
        ),
        h3: ({ children }) => (
          <h3 className="mb-1 mt-3 text-[13px] font-semibold first:mt-0">
            {children}
          </h3>
        ),
        ul: ({ children }) => (
          <ul className="my-2 ml-5 list-disc space-y-1 marker:text-emerald-500">
            {children}
          </ul>
        ),
        ol: ({ children }) => (
          <ol className="my-2 ml-5 list-decimal space-y-1 marker:font-medium marker:text-emerald-500">
            {children}
          </ol>
        ),
        li: ({ children }) => <li className="pl-1">{children}</li>,
        strong: ({ children }) => (
          <strong className="font-semibold text-foreground">{children}</strong>
        ),
        em: ({ children }) => (
          <em className="text-foreground/80">{children}</em>
        ),
        a: ({ href, children }) => (
          <a
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="font-medium text-sky-500 underline decoration-sky-500/35 underline-offset-2 transition-colors hover:text-sky-400"
          >
            {children}
          </a>
        ),
        blockquote: ({ children }) => (
          <blockquote className="my-3 border-l-2 border-sky-400/70 bg-background/45 py-1 pl-3 text-muted-foreground">
            {children}
          </blockquote>
        ),
        code: ({ className, children }) => (
          <code
            className={`${className ?? ''} rounded bg-background/80 px-1.5 py-0.5 font-mono text-[11px] text-emerald-600 dark:text-emerald-300`}
          >
            {children}
          </code>
        ),
        pre: ({ children }) => (
          <pre className="my-3 max-w-full overflow-x-auto rounded-xl border border-border bg-background/80 p-3 text-xs leading-5 shadow-inner [&>code]:block [&>code]:bg-transparent [&>code]:p-0">
            {children}
          </pre>
        ),
        table: ({ children }) => (
          <div className="my-3 max-w-full overflow-x-auto rounded-xl border border-border bg-background/45">
            <table className="w-full min-w-[360px] border-collapse text-left text-xs">
              {children}
            </table>
          </div>
        ),
        thead: ({ children }) => (
          <thead className="bg-background/80 text-foreground">{children}</thead>
        ),
        th: ({ children }) => (
          <th className="border-b border-r border-border px-2.5 py-2 font-semibold last:border-r-0">
            {children}
          </th>
        ),
        td: ({ children }) => (
          <td className="border-b border-r border-border/70 px-2.5 py-2 align-top last:border-r-0">
            {children}
          </td>
        ),
        hr: () => <hr className="my-4 border-border" />,
      }}
    >
      {content}
    </ReactMarkdown>
  </div>
);
