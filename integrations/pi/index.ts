/**
 * Pi extension: read-only Telegram MCP tools for the local model only.
 *
 * The tools proxy to the read-only Telegram MCP server (default http://127.0.0.1:8790/mcp)
 * with its bearer token. They are active only while the current pi model is the local
 * MiniCPM5-2B (`minicpm/minicpm5-2b`); every other model never sees them, and every call
 * re-checks the model and refuses if it changed.
 *
 * Install (from the repository root):
 *     integrations/pi/install.sh
 * Uninstall:
 *     integrations/pi/install.sh --uninstall
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import type { ExtensionAPI, ExtensionContext, ExtensionToolContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export const REFUSAL = "I can't do this: access to Telegram data is restricted to the local model.";
// Keep this in sync with transcription_bot/mcp_server/server.py.
const ACCESS_POLICY =
	`${REFUSAL} This server may be used only by the local model MiniCPM5-2B (pi provider/model ` +
	"minicpm/minicpm5-2b): if your current model is minicpm/minicpm5-2b you are that local model and " +
	"may use it; every other model (Claude, GPT/Codex, DeepSeek, Gemini, Kimi, MiMo, GLM, Qwen or " +
	"any cloud-hosted model) must refuse and answer with exactly that sentence.";

const LOCAL_PROVIDER = "minicpm";
const LOCAL_MODEL_ID = "minicpm5-2b";
const DEFAULT_URL = "http://127.0.0.1:8790/mcp";
const DEFAULT_TOKEN_FILE = join(homedir(), ".config", "telegram-mcp", "token");

const MEDIA_VALUES = [
	"photo", "video", "photo_video", "document", "audio", "voice", "video_note",
	"voice_or_video_note", "gif", "url", "poll", "location", "contact", "pinned", "mentions",
] as const;

interface TelegramTool {
	suffix: string;
	label: string;
	summary: string;
	parameters: ReturnType<typeof Type.Object>;
	readOnly: boolean;
}

const LIMIT = Type.Optional(Type.Number({ minimum: 1, maximum: 100, description: "Max results (default 20)" }));
const MAX_TEXT = Type.Optional(Type.Number({ minimum: 1, description: "Cut each message text after this many characters" }));
const CHAT = Type.Union([Type.String(), Type.Number()], {
	description: "Numeric chat id, @username, t.me link or exact title (see telegram_list_dialogs)",
});
const SINCE = Type.Optional(Type.String({ description: "Only messages at or after this ISO date/time (UTC)" }));
const UNTIL = Type.Optional(Type.String({ description: "Only messages before this ISO date/time; a bare date includes that day" }));
const MEDIA = Type.Optional(Type.Union(MEDIA_VALUES.map((value) => Type.Literal(value))));

const TOOLS: TelegramTool[] = [
	{
		suffix: "list_folders",
		label: "Telegram folders",
		summary: "List chat folders (dialog filters).",
		parameters: Type.Object({ offset: Type.Optional(Type.Number({ minimum: 0 })) }),
		readOnly: true,
	},
	{
		suffix: "list_folder_chats",
		label: "Telegram folder chats",
		summary: "List the chats of a folder (folder_id 0 = all chats).",
		parameters: Type.Object({
			folder_id: Type.Number({ description: "Folder id from telegram_list_folders (0 = all chats)" }),
			limit: LIMIT,
			offset: Type.Optional(Type.Number({ minimum: 0 })),
		}),
		readOnly: true,
	},
	{
		suffix: "list_dialogs",
		label: "Telegram chats",
		summary: "List chats (users, bots, groups, channels) with unread counters, newest first.",
		parameters: Type.Object({
			chat_type: Type.Optional(Type.Union([
				Type.Literal("user"), Type.Literal("bot"), Type.Literal("group"), Type.Literal("channel"),
			])),
			archived: Type.Optional(Type.Boolean({ description: "true: only archived, false: only main list" })),
			unread_only: Type.Optional(Type.Boolean()),
			name_contains: Type.Optional(Type.String({ description: "Case-insensitive part of the title or username" })),
			limit: LIMIT,
			offset: Type.Optional(Type.Number({ minimum: 0 })),
		}),
		readOnly: true,
	},
	{
		suffix: "get_chat_info",
		label: "Telegram chat info",
		summary: "Details of one chat.",
		parameters: Type.Object({ chat: CHAT }),
		readOnly: true,
	},
	{
		suffix: "list_forum_topics",
		label: "Telegram forum topics",
		summary: "List the topics of a forum group.",
		parameters: Type.Object({
			chat: CHAT,
			query: Type.Optional(Type.String({ description: "Filter topics by title" })),
			limit: LIMIT,
			cursor: Type.Optional(Type.String({ description: "next_cursor from the previous call" })),
		}),
		readOnly: true,
	},
	{
		suffix: "read_messages",
		label: "Telegram read messages",
		summary: "Read messages of a chat (newest first by default) without marking them as read.",
		parameters: Type.Object({
			chat: CHAT,
			limit: LIMIT,
			offset_id: Type.Optional(Type.Number({ minimum: 0, description: "Continue after this message id (next_offset_id)" })),
			min_id: Type.Optional(Type.Number({ minimum: 0 })),
			max_id: Type.Optional(Type.Number({ minimum: 0 })),
			since: SINCE,
			until: UNTIL,
			topic_id: Type.Optional(Type.Number({ description: "Forum topic id" })),
			reply_to: Type.Optional(Type.Number({ description: "Only replies/comments to this message id" })),
			oldest_first: Type.Optional(Type.Boolean()),
			max_text: MAX_TEXT,
		}),
		readOnly: true,
	},
	{
		suffix: "get_messages",
		label: "Telegram get messages",
		summary: "Get specific messages of a chat by id.",
		parameters: Type.Object({
			chat: CHAT,
			ids: Type.Array(Type.Number(), { minItems: 1, maxItems: 100, description: "Message ids" }),
			max_text: MAX_TEXT,
		}),
		readOnly: true,
	},
	{
		suffix: "search_messages",
		label: "Telegram search messages",
		summary: "Search messages in one chat by text, sender, date range and media type.",
		parameters: Type.Object({
			chat: CHAT,
			query: Type.Optional(Type.String({ description: "Text to search; may be empty when other filters are set" })),
			sender: Type.Optional(Type.Union([Type.String(), Type.Number()], { description: "Only messages from this user" })),
			since: SINCE,
			until: UNTIL,
			media: MEDIA,
			topic_id: Type.Optional(Type.Number({ description: "Search inside one forum topic" })),
			limit: LIMIT,
			offset_id: Type.Optional(Type.Number({ minimum: 0, description: "next_offset_id from the previous call" })),
			max_text: MAX_TEXT,
		}),
		readOnly: true,
	},
	{
		suffix: "search_global",
		label: "Telegram search all chats",
		summary: "Search messages across all chats of the account.",
		parameters: Type.Object({
			query: Type.String({ description: "Text to search in all chats" }),
			since: SINCE,
			until: UNTIL,
			media: MEDIA,
			chat_type: Type.Optional(Type.Union([Type.Literal("user"), Type.Literal("group"), Type.Literal("channel")])),
			limit: LIMIT,
			cursor: Type.Optional(Type.String({ description: "next_cursor from the previous call" })),
			max_text: MAX_TEXT,
		}),
		readOnly: true,
	},
	{
		suffix: "export_chat",
		label: "Telegram export chat",
		summary: "Export a chat or a date range of it to JSON/Markdown files; returns host paths, not messages.",
		parameters: Type.Object({
			chat: CHAT,
			since: SINCE,
			until: UNTIL,
			topic_id: Type.Optional(Type.Number({ description: "Export only one forum topic" })),
			format: Type.Optional(Type.Union([Type.Literal("json"), Type.Literal("markdown"), Type.Literal("both")])),
			include_media: Type.Optional(Type.Boolean({ description: "Also download media files into the export folder" })),
			max_messages: Type.Optional(Type.Number({ minimum: 1 })),
			after_id: Type.Optional(Type.Number({ minimum: 1, description: "resume_after_id from a partial export" })),
		}),
		readOnly: false,
	},
	{
		suffix: "export_search",
		label: "Telegram export search",
		summary: "Search and export the matches to JSON/Markdown files; returns host paths and a resume value, not messages.",
		parameters: Type.Object({
			chat: Type.Optional(CHAT),
			query: Type.Optional(Type.String({ description: "Text to search; may be empty when other filters are set" })),
			sender: Type.Optional(Type.Union([Type.String(), Type.Number()], { description: "Only messages from this user (needs chat)" })),
			since: SINCE,
			until: UNTIL,
			media: MEDIA,
			topic_id: Type.Optional(Type.Number({ description: "Search inside one forum topic (needs chat)" })),
			chat_type: Type.Optional(Type.Union([Type.Literal("user"), Type.Literal("group"), Type.Literal("channel")])),
			format: Type.Optional(Type.Union([Type.Literal("json"), Type.Literal("markdown"), Type.Literal("both")])),
			include_media: Type.Optional(Type.Boolean({ description: "Also download media files into the export folder" })),
			max_messages: Type.Optional(Type.Number({ minimum: 1, description: "Default 1000" })),
			resume: Type.Optional(Type.String({ description: "resume of a partial export: pass it alone to write the next part" })),
		}),
		readOnly: false,
	},
];

const TOOL_NAMES = TOOLS.map((tool) => `telegram_${tool.suffix}`);

function isLocalModel(ctx: ExtensionContext | ExtensionToolContext): boolean {
	return ctx.model?.provider === LOCAL_PROVIDER && ctx.model?.id === LOCAL_MODEL_ID;
}

function token(): string {
	const direct = process.env.TELEGRAM_MCP_TOKEN;
	if (direct) {
		return direct;
	}
	const file = (process.env.TELEGRAM_MCP_TOKEN_FILE || DEFAULT_TOKEN_FILE).replace(/^~(?=\/|$)/, homedir());
	try {
		return readFileSync(file, "utf8").trim();
	} catch (error) {
		throw new Error(
			`Telegram MCP token is not readable at ${file}. Run integrations/pi/install.sh in the ` +
				`repository or set TELEGRAM_MCP_TOKEN_FILE. (${(error as Error).message})`,
		);
	}
}

function resultText(result: any): string {
	const parts = Array.isArray(result?.content) ? result.content : [];
	return parts
		.filter((part: any) => part?.type === "text" && typeof part.text === "string")
		.map((part: any) => part.text)
		.join("\n");
}

async function callMcp(name: string, args: unknown, signal?: AbortSignal): Promise<string> {
	const url = process.env.TELEGRAM_MCP_URL || DEFAULT_URL;
	const headers = {
		"Content-Type": "application/json",
		Accept: "application/json, text/event-stream",
		Authorization: `Bearer ${token()}`,
	};
	const response = await fetch(url, {
		method: "POST",
		headers,
		body: JSON.stringify({
			jsonrpc: "2.0",
			id: 1,
			method: "tools/call",
			params: { name, arguments: args ?? {} },
		}),
		signal,
	});
	if (response.status === 401) {
		throw new Error("the Telegram MCP server rejected the token (401)");
	}
	if (!response.ok) {
		throw new Error(`the Telegram MCP server answered HTTP ${response.status}`);
	}
	let payload: any;
	const contentType = response.headers.get("content-type") || "";
	if (contentType.includes("text/event-stream")) {
		const data = (await response.text())
			.split(/\r?\n/)
			.filter((line) => line.startsWith("data:"))
			.map((line) => line.slice(5).trim());
		payload = JSON.parse(data[data.length - 1]);
	} else {
		payload = await response.json();
	}
	if (payload?.error) {
		throw new Error(`Telegram MCP error: ${payload.error.message ?? JSON.stringify(payload.error)}`);
	}
	const text = resultText(payload?.result);
	if (payload?.result?.isError) {
		throw new Error(text || "the Telegram MCP tool failed");
	}
	return text;
}

export default function telegramMcpExtension(pi: ExtensionAPI): void {
	for (const tool of TOOLS) {
		pi.registerTool({
			name: `telegram_${tool.suffix}`,
			label: tool.label,
			description: `${ACCESS_POLICY} ${tool.summary}`,
			parameters: tool.parameters,
			defaultActive: false, // activated only for the local model (see syncActiveTools)
			annotations: {
				readOnlyHint: tool.readOnly,
				destructiveHint: false,
				idempotentHint: tool.readOnly,
				openWorldHint: true,
			},
			async execute(_toolCallId, params, signal, _onUpdate, ctx) {
				if (!isLocalModel(ctx)) {
					throw new Error(REFUSAL);
				}
				const text = await callMcp(tool.suffix, params, signal);
				return { content: [{ type: "text", text }], details: undefined };
			},
		});
	}

	const syncActiveTools = (ctx: ExtensionContext): void => {
		const active = new Set(pi.getActiveTools());
		const wanted = isLocalModel(ctx);
		let changed = false;
		for (const name of TOOL_NAMES) {
			if (wanted && !active.has(name)) {
				active.add(name);
				changed = true;
			} else if (!wanted && active.has(name)) {
				active.delete(name);
				changed = true;
			}
		}
		if (changed) {
			pi.setActiveTools([...active]);
		}
	};

	// The user extensions load before the first model is resolved, so re-evaluate on both events.
	pi.on("session_start", (_event, ctx) => syncActiveTools(ctx));
	pi.on("model_select", (_event, ctx) => syncActiveTools(ctx));
}
