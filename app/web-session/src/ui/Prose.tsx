import { memo, type ReactNode } from "react";

import { cx } from "./cx";
import { parseInline, parseMarkdown, type Block, type Inline } from "./markdown";

function inline(nodes: Inline[], links = true): ReactNode {
  return nodes.map((node, index) => {
    switch (node.t) {
      case "text":
        return node.text;
      case "code":
        return (
          <code key={index} className="sv-inline-code rounded-control bg-subtle px-1 py-0.5 font-mono break-words">
            {node.text}
          </code>
        );
      case "strong":
        return (
          <strong key={index} className="font-semibold">
            {inline(node.children, links)}
          </strong>
        );
      case "em":
        return <em key={index}>{inline(node.children, links)}</em>;
      case "link":
        // A line of text that must not carry the person anywhere (a request's prompt): the words, no anchor.
        if (!links) return <span key={index}>{inline(node.children, links)}</span>;
        return (
          <a key={index} className="text-link underline decoration-border-strong underline-offset-2 hover:decoration-current" href={node.href} rel="noopener noreferrer" target="_blank">
            {inline(node.children)}
          </a>
        );
    }
  });
}

const HEADING_SIZE = ["text-lg", "text-base", "text-base", "text-sm", "text-sm", "text-sm"] as const;

function blocks(list: Block[], tight = false): ReactNode {
  return list.map((block, index) => {
    switch (block.t) {
      case "paragraph":
        return (
          <p key={index} className={tight ? undefined : "my-2 first:mt-0 last:mb-0"}>
            {inline(block.children)}
          </p>
        );
      case "heading": {
        const Tag = `h${Math.min(block.level + 1, 6)}` as "h2";
        return (
          <Tag key={index} className={cx("mt-4 mb-1.5 font-semibold first:mt-0", HEADING_SIZE[block.level - 1])}>
            {inline(block.children)}
          </Tag>
        );
      }
      case "code":
        return (
          <pre key={index} className="my-2 overflow-x-auto rounded-block border border-border bg-subtle p-3 font-mono text-xs leading-5" data-lang={block.lang || undefined}>
            <code>{block.text}</code>
          </pre>
        );
      case "list": {
        const List = block.ordered ? "ol" : "ul";
        return (
          <List
            key={index}
            className={cx("my-2 space-y-1 pl-5 first:mt-0 last:mb-0", block.ordered ? "list-decimal" : "list-disc")}
            {...(block.ordered && block.start !== 1 ? { start: block.start } : {})}
          >
            {block.items.map((item, at) => (
              <li key={at}>
                {inline(item.children)}
                {blocks(item.blocks, true)}
              </li>
            ))}
          </List>
        );
      }
      case "quote":
        return (
          <blockquote key={index} className="my-2 border-l-2 border-border-strong pl-3 text-secondary">
            {blocks(block.blocks)}
          </blockquote>
        );
      case "rule":
        return <hr key={index} className="my-3 border-border" />;
      case "table":
        if (block.head.length === 0) return <TableCut key={index} cut={block.cut ?? { cols: 0, rows: 0, text: "" }} whole />;
        return (
          <div key={index} className="my-2 overflow-x-auto">
            <table className="w-full border-collapse text-left text-xs">
              <thead>
                <tr>
                  {block.head.map((cell, at) => (
                    <th key={at} className="border-b border-border-strong px-2 py-1 font-medium" style={align(block.align[at])}>
                      {inline(cell)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, at) => (
                  <tr key={at}>
                    {row.map((cell, c) => (
                      <td key={c} className="border-b border-divider px-2 py-1 align-top" style={align(block.align[c])}>
                        {inline(cell)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {block.cut !== null ? <TableCut cut={block.cut} /> : null}
          </div>
        );
    }
  });
}

function TableCut({ cut, whole = false }: { cut: { cols: number; rows: number; text: string }; whole?: boolean }) {
  // A table left out whole says so as it is; one that lost some says how much more there was.
  const more = whole ? "" : "more ";
  const parts = [cut.cols > 0 ? `${cut.cols} ${more}${cut.cols === 1 ? "column" : "columns"}` : "", cut.rows > 0 ? `${cut.rows} ${more}${cut.rows === 1 ? "row" : "rows"}` : ""].filter(Boolean);
  return (
    <div className="my-2" data-table-cut>
      <p className="text-xs text-muted">
        Table cut: {whole && parts.length === 0 ? "all of it" : parts.join(" and ")} not shown{whole ? " (this reply has more tables than this view draws)" : ""}.
      </p>
      {cut.text !== "" ? <p className="mt-0.5 text-xs break-words whitespace-pre-wrap text-secondary">{cut.text}</p> : null}
    </div>
  );
}

function align(value: "left" | "right" | "center" | null | undefined) {
  return value === null || value === undefined ? undefined : { textAlign: value };
}

/** An agent's reply drawn as prose. The text is never HTML: it becomes React elements. Drawn again only when its text changes. */
export const Prose = memo(function Prose({ text, className }: { text: string; className?: string }) {
  return <div className={cx("sv-prose text-sm leading-6 break-words", className)}>{blocks(parseMarkdown(text))}</div>;
});

/**
 * One run of an agent's text with the inline formatting the timeline reads (code, bold, italic) and nothing
 * else: no block, no table, and no link (the words of a link stay, the anchor does not). Only the first
 * `readMax` characters are read for formatting, which caps the nodes it can make at about two thirds of that
 * many; the rest is plain text in one node. Line breaks are the container's (`whitespace-pre-wrap`).
 */
export function InlineText({ text, readMax = 2000 }: { text: string; readMax?: number }) {
  const head = text.length > readMax ? text.slice(0, readMax) : text;
  return (
    <>
      {inline(parseInline(head), false)}
      {text.length > readMax ? text.slice(readMax) : null}
    </>
  );
}
