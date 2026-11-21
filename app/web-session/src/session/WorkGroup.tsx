import { useCallback, useState } from "react";

import type { CheckpointItem, ThinkingItem, ToolItem } from "../core";
import { Collapsible, cx, Icon, Spinner, type IconName } from "../ui";
import { clip, diffLines, diffStat, formatDuration, plural } from "./format";
import type { WorkEntry, WorkGroup } from "./model";
import { toolTitle } from "./model";

/** Open and closed state by id: a person's own choice, kept while the view lives, wins over the default. */
export function useToggles() {
  const [choices, setChoices] = useState<Record<string, boolean>>({});
  const isOpen = useCallback((id: string, fallback: boolean) => choices[id] ?? fallback, [choices]);
  const set = useCallback((id: string, open: boolean) => setChoices((held) => ({ ...held, [id]: open })), []);
  return { isOpen, set };
}

export type Toggles = ReturnType<typeof useToggles>;

const SHOWN_CHARS = 6000;
const SHOWN_DIFF_LINES = 300;

function toolIcon(name: string | undefined): IconName {
  const lower = (name ?? "").toLowerCase();
  if (/^(bash|shell|exec|command|run)/.test(lower)) return "terminal";
  if (/(edit|write|read|file|patch|notebook)/.test(lower)) return "file";
  return "tool";
}

function Text({ text, label }: { text: string; label: string }) {
  const { text: shown, hidden } = clip(text, SHOWN_CHARS);
  return (
    <div>
      <p className="mb-1 text-xs text-muted">{label}</p>
      <pre className="max-h-64 overflow-auto rounded-block border border-border bg-subtle p-2.5 font-mono text-xs leading-5 whitespace-pre-wrap break-words">
        <code>{shown}</code>
      </pre>
      {hidden > 0 ? <p className="mt-1 text-xs text-muted">{plural(hidden, "more character")} not shown.</p> : null}
    </div>
  );
}

function stringify(input: unknown): string {
  if (typeof input === "string") return input;
  try {
    return JSON.stringify(input, null, 2) ?? "";
  } catch {
    return String(input);
  }
}

function Diff({ diff }: { diff: string }) {
  const lines = diffLines(diff);
  const shown = lines.slice(0, SHOWN_DIFF_LINES);
  return (
    <>
      <pre className="max-h-64 overflow-auto rounded-block border border-border bg-subtle py-1.5 font-mono text-xs leading-5">
        <code className="block min-w-max">
          {shown.map((line, index) => (
            <span
              key={index}
              className={cx(
                "block px-2.5",
                line.kind === "add" && "text-success",
                line.kind === "remove" && "text-danger-text",
                (line.kind === "hunk" || line.kind === "header") && "text-muted",
              )}
            >
              {line.text === "" ? " " : line.text}
            </span>
          ))}
        </code>
      </pre>
      {lines.length > shown.length ? <p className="mt-1 text-xs text-muted">{plural(lines.length - shown.length, "more line")} not shown.</p> : null}
    </>
  );
}

function ToolDetails({ tool }: { tool: ToolItem }) {
  const input = tool.input === undefined ? "" : stringify(tool.input);
  return (
    <div className="space-y-2.5 pt-1 pb-2 pl-7">
      {input !== "" ? <Text label="Input" text={input} /> : null}
      {tool.status === "running" && tool.text !== undefined && tool.text !== "" ? <Text label="Progress" text={tool.text} /> : null}
      {tool.fileEdits?.map((edit) => {
        const stat = diffStat(edit.diff);
        return (
          <div key={`${edit.kind}-${edit.path}`}>
            <p className="mb-1 flex flex-wrap items-baseline gap-x-2 text-xs">
              <span className="text-muted">{edit.kind === "add" ? "Added " : edit.kind === "delete" ? "Deleted " : "Changed "}</span>
              <span className="min-w-0 font-medium break-all">{edit.path} </span>
              {edit.diff !== undefined ? (
                <span className="text-muted">
                  <span className="text-success">+{stat.added}</span> <span className="text-danger-text">−{stat.removed}</span>
                </span>
              ) : null}
            </p>
            {edit.diff !== undefined && edit.diff !== "" ? <Diff diff={edit.diff} /> : null}
          </div>
        );
      })}
      {tool.output !== undefined && tool.output !== "" ? <Text label={tool.status === "error" ? "Error output" : "Output"} text={tool.output} /> : null}
      {input === "" && (tool.output ?? "") === "" && (tool.fileEdits ?? []).length === 0 ? <p className="text-xs text-muted">No details were recorded for this step.</p> : null}
    </div>
  );
}

function StatusMark({ status }: { status: ToolItem["status"] }) {
  switch (status) {
    case "running":
      return <Spinner />;
    case "ok":
      return <Icon className="size-3.5 shrink-0 text-success" name="check" />;
    case "error":
      return <Icon className="size-3.5 shrink-0 text-danger-text" name="alert" />;
    case "cancelled":
      return <Icon className="size-3.5 shrink-0 text-muted" name="x" />;
  }
}

const STATUS_WORD: Record<ToolItem["status"], string> = { running: "Running", ok: "Done", error: "Failed", cancelled: "Cancelled" };

function ToolRow({ tool, toggles }: { tool: ToolItem; toggles: Toggles }) {
  const id = `tool-${tool.seq}`;
  const open = toggles.isOpen(id, false);
  return (
    <Collapsible
      buttonClassName="min-h-8 px-1.5 py-1 text-sm"
      className="sv-tool"
      header={
        <>
          <Icon className="size-3.5 shrink-0 text-muted" name={toolIcon(tool.name)} />
          <span className="min-w-0 flex-1 truncate">{toolTitle(tool)}</span>
          <StatusMark status={tool.status} />
          <span className="sr-only"> {STATUS_WORD[tool.status]}</span>
        </>
      }
      label={`Details of ${toolTitle(tool)}`}
      onToggle={(next) => toggles.set(id, next)}
      open={open}
    >
      <ToolDetails tool={tool} />
    </Collapsible>
  );
}

function ThinkingRow({ item, toggles }: { item: ThinkingItem; toggles: Toggles }) {
  const id = `thought-${item.seq}`;
  const open = toggles.isOpen(id, false);
  return (
    <Collapsible
      buttonClassName="min-h-8 px-1.5 py-1 text-sm"
      header={
        <>
          <Icon className="size-3.5 shrink-0 text-muted" name="thought" />
          <span className="min-w-0 flex-1 truncate text-secondary">
            Thought{item.durationS !== undefined && item.durationS >= 1 ? ` for ${formatDuration(item.durationS)}` : ""}
          </span>
        </>
      }
      label="Thinking"
      onToggle={(next) => toggles.set(id, next)}
      open={open}
    >
      <p className="pt-1 pb-2 pl-7 text-sm leading-6 whitespace-pre-wrap text-secondary break-words">{item.text}</p>
    </Collapsible>
  );
}

function CheckpointRow({ item }: { item: CheckpointItem }) {
  const what = item.what === "restored" ? "Restored files to a checkpoint" : "Checkpoint saved";
  return (
    <p className="flex min-h-8 items-center gap-2 py-1 pr-1.5 pl-7 text-sm text-secondary">
      <Icon className="size-3.5 shrink-0 text-muted" name="checkpoint" />
      <span className="min-w-0 flex-1 truncate">
        {what}
        {item.files !== undefined ? ` · ${plural(item.files, "file")}` : ""}
        {item.note !== undefined && item.note !== "" ? ` · ${item.note}` : ""}
      </span>
    </p>
  );
}

function Entry({ entry, toggles }: { entry: WorkEntry; toggles: Toggles }) {
  switch (entry.kind) {
    case "tool":
      return <ToolRow tool={entry} toggles={toggles} />;
    case "thinking":
      return <ThinkingRow item={entry} toggles={toggles} />;
    case "checkpoint":
      return <CheckpointRow item={entry} />;
  }
}

function headline(group: WorkGroup): string {
  if (group.running) return "Working";
  return group.span !== null && group.span >= 1 ? `Worked for ${formatDuration(group.span)}` : "Worked";
}

function detail(group: WorkGroup): string {
  const parts: string[] = [];
  if (group.tools > 0) parts.push(plural(group.tools, "tool call"));
  if (group.thoughts > 0) parts.push(plural(group.thoughts, "thought"));
  return parts.join(", ");
}

/**
 * The steps an agent took between two pieces of prose, collapsed to one line ("Worked for 2m 29s,
 * 3 tool calls") once it is done and open while it is still going. A person's own toggle wins over
 * both, and stays: the group does not close itself under someone reading it.
 */
export function WorkGroupView({ group, toggles }: { group: WorkGroup; toggles: Toggles }) {
  const open = toggles.isOpen(group.id, group.running);
  return (
    <Collapsible
      buttonClassName="min-h-9 px-1.5 py-1.5 text-sm"
      className="rounded-card border border-border bg-subtle p-0.5"
      header={
        <>
          {group.running ? <Spinner /> : <Icon className="size-3.5 shrink-0 text-muted" name="tool" />}
          <span className="font-medium">{headline(group)}</span>
          <span className="min-w-0 flex-1 truncate text-secondary">{detail(group) === "" ? "" : ` · ${detail(group)}`}</span>
          {group.failed > 0 ? <span className="shrink-0 text-xs text-danger-text"> {group.failed} failed</span> : null}
        </>
      }
      label="Steps"
      onToggle={(next) => toggles.set(group.id, next)}
      open={open}
    >
      <div className="px-1 pb-1">
        {group.entries.map((entry) => (
          <Entry key={`${entry.kind}-${entry.seq}`} entry={entry} toggles={toggles} />
        ))}
      </div>
    </Collapsible>
  );
}
