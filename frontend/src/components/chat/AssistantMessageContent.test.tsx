import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { AssistantMessageContent } from './AssistantMessageContent';

describe('AssistantMessageContent', () => {
  it('renders common Copilot markdown as structured, safe HTML', () => {
    const html = renderToStaticMarkup(
      <AssistantMessageContent
        content={`## Pipeline review

- Keep the **source**
- Add \`validation\`

| Step | Status |
| --- | --- |
| Source | Ready |

[Documentation](https://example.com)

<script>alert('unsafe')</script>`}
      />,
    );

    expect(html).toContain('<h2');
    expect(html).toContain('<ul');
    expect(html).toContain('<strong');
    expect(html).toContain('<code');
    expect(html).toContain('<table');
    expect(html).toContain('target="_blank"');
    expect(html).not.toContain('<script>');
  });
});
