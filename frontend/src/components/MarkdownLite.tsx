import type { ReactNode } from "react";

type MdBlock =
  | { type: "text"; text: string }
  | { type: "code"; lang: string | null; code: string };

function parseBlocks(src: string): MdBlock[] {
  const text = (src || "").replace(/\r\n/g, "\n");
  const blocks: MdBlock[] = [];
  const re = /```([a-zA-Z0-9_-]+)?\n([\s\S]*?)```/g;
  let last = 0;
  for (;;) {
    const m = re.exec(text);
    if (!m) break;
    if (m.index > last) {
      blocks.push({ type: "text", text: text.slice(last, m.index) });
    }
    const lang = (m[1] || "").trim() || null;
    blocks.push({ type: "code", lang, code: m[2] || "" });
    last = re.lastIndex;
  }
  if (last < text.length) blocks.push({ type: "text", text: text.slice(last) });
  return blocks;
}

function renderInline(text: string): ReactNode[] {
  const parts = text.split(/(`[^`]*`)/g);
  const out: ReactNode[] = [];
  let key = 0;
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      out.push(
        <code key={`c${key++}`} className="mdInlineCode">
          {part.slice(1, -1)}
        </code>
      );
      continue;
    }
    const boldParts = part.split(/(\*\*[^*]+\*\*)/g);
    for (const bp of boldParts) {
      if (!bp) continue;
      if (bp.startsWith("**") && bp.endsWith("**") && bp.length >= 4) {
        out.push(
          <strong key={`b${key++}`} className="mdStrong">
            {bp.slice(2, -2)}
          </strong>
        );
      } else {
        out.push(<span key={`t${key++}`}>{bp}</span>);
      }
    }
  }
  return out;
}

function renderTextBlock(text: string): ReactNode {
  const lines = (text || "").replace(/\r\n/g, "\n").split("\n");
  const nodes: ReactNode[] = [];
  let i = 0;
  let key = 0;

  const isBlank = (s: string) => !s.trim();
  const isListLine = (s: string) => /^[-*+]\s+/.test(s);
  const headingMatch = (s: string) => /^(#{1,3})\s+(.+)$/.exec(s);

  while (i < lines.length) {
    while (i < lines.length && isBlank(lines[i])) i++;
    if (i >= lines.length) break;

    const h = headingMatch(lines[i]);
    if (h) {
      const level = h[1].length;
      const content = h[2] || "";
      nodes.push(
        <div key={`h${key++}`} className={`mdHeading mdH${level}`}>
          {renderInline(content)}
        </div>
      );
      i++;
      continue;
    }

    if (isListLine(lines[i])) {
      const items: string[] = [];
      while (i < lines.length && isListLine(lines[i])) {
        items.push(lines[i].replace(/^[-*+]\s+/, ""));
        i++;
      }
      nodes.push(
        <ul key={`ul${key++}`} className="mdList">
          {items.map((it, idx) => (
            <li key={idx} className="mdListItem">
              {renderInline(it)}
            </li>
          ))}
        </ul>
      );
      continue;
    }

    const para: string[] = [];
    while (i < lines.length && !isBlank(lines[i]) && !headingMatch(lines[i]) && !isListLine(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    nodes.push(
      <p key={`p${key++}`} className="mdPara">
        {para.map((ln, idx) => (
          <span key={idx}>
            {renderInline(ln)}
            {idx < para.length - 1 ? <br /> : null}
          </span>
        ))}
      </p>
    );
  }

  return <div className="mdText">{nodes}</div>;
}

export default function MarkdownLite(props: { source: string; className?: string }) {
  const blocks = parseBlocks(props.source);
  return (
    <div className={`mdRoot ${props.className || ""}`.trim()}>
      {blocks.map((b, idx) => {
        if (b.type === "code") {
          return (
            <pre key={idx} className="mdCode">
              <code data-lang={b.lang || undefined}>{b.code}</code>
            </pre>
          );
        }
        return <div key={idx}>{renderTextBlock(b.text)}</div>;
      })}
    </div>
  );
}

