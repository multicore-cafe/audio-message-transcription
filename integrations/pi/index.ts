/**
 * Pi extension: read-only Telegram MCP tools for allow-listed models only.
 *
 * The tools proxy to the read-only Telegram MCP server (default http://127.0.0.1:8790/mcp)
 * with its bearer token. They are active only while the current pi provider/model is listed in
 * the allowed-models file (default ~/.config/telegram-mcp/allowed-models, override with
 * TELEGRAM_MCP_ALLOWED_MODELS_FILE); every other model never sees them, and every call
 * re-reads the file and refuses if the model is not allowed.
 *
 * The two Saved Messages tools (telegram_send_to_saved, telegram_forward_to_saved) exist on the
 * server only with MCP_SAVED_MESSAGES_WRITE=1. They are activated only for an allowed model and
 * only when the server's tool list contains them; an unreachable server activates neither.
 *
 * Fail closed: a missing, unreadable, empty or invalid file allows no model at all and every
 * call is refused with a message naming the file.
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

export const REFUSAL = "I can't do this: access to Telegram data is restricted to the models allowed by the owner.";
// Keep the policy wording in sync with transcription_bot/mcp_server/allowlist.py.
const CREATE_HINT =
	"Create it with `integrations/pi/install.sh --allow <provider/model>` (repeatable), or " +
	"write one entry per line, e.g. `someprovider/*`.";

const DEFAULT_ALLOWED_MODELS_FILE = join(homedir(), ".config", "telegram-mcp", "allowed-models");
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
	// Writes into the own Saved Messages: active only when the server lists the tool.
	savedWrite?: boolean;
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
	{
		suffix: "send_to_saved",
		label: "Telegram save note",
		summary:
			"Send a plain-text note into the owner's own Saved Messages (nowhere else); split into 4096-character " +
			"messages, at most 16000 characters per call. Returns the new message ids.",
		parameters: Type.Object({
			text: Type.String({ minLength: 1, maxLength: 16000, description: "Plain text (no formatting is parsed)" }),
		}),
		readOnly: false,
		savedWrite: true,
	},
	{
		suffix: "forward_to_saved",
		label: "Telegram forward to Saved Messages",
		summary:
			"Forward messages of a chat into the owner's own Saved Messages (nowhere else); up to 100 ids per call. " +
			"Returns the new message ids.",
		parameters: Type.Object({
			chat: CHAT,
			message_ids: Type.Array(Type.Number(), { minItems: 1, maxItems: 100, description: "Ids of the messages to forward" }),
		}),
		readOnly: false,
		savedWrite: true,
	},
];

const SAVED_WRITE_SUFFIXES = TOOLS.filter((tool) => tool.savedWrite).map((tool) => tool.suffix);
const SERVER_LIST_TIMEOUT_MS = 5000;

interface AllowList {
	entries: string[];
	providers: Set<string>;
}

// The policy wording is generated from the allow-list; keep it in sync with
// transcription_bot/mcp_server/allowlist.py (access_policy).
export function accessPolicy(models: string): string {
	return (
		`${REFUSAL} Allowed models: ${models}. Only a model whose pi provider/model matches ` +
		"this list may use the Telegram tools; every other model must refuse and answer with exactly " +
		"that sentence."
	);
}

function noAllowListPolicy(file: string): string {
	return (
		`${REFUSAL} No valid allowed-models file is present at ${file}; no model may use the ` +
		"Telegram tools until one is created with valid entries."
	);
}

function allowedModelsFile(): string {
	return (process.env.TELEGRAM_MCP_ALLOWED_MODELS_FILE || DEFAULT_ALLOWED_MODELS_FILE).replace(/^~(?=\/|$)/, homedir());
}

function parseAllowList(text: string, source: string): AllowList {
	const entries: string[] = [];
	const providers = new Set<string>();
	const seen = new Set<string>();
	text.split(/\r?\n/).forEach((raw, index) => {
		const line = raw.trim();
		if (!line || line.startsWith("#")) {
			return;
		}
		const location = `${source}: line ${index + 1}`;
		if (!line.includes("/") || /\s/.test(line)) {
			throw new Error(`${location}: expected \`provider/model-id\` or \`provider/*\`, got ${JSON.stringify(line)}`);
		}
		const slash = line.indexOf("/");
		const provider = line.slice(0, slash);
		const model = line.slice(slash + 1);
		if (!provider || !model || provider === "*") {
			throw new Error(`${location}: expected \`provider/model-id\` or \`provider/*\`, got ${JSON.stringify(line)}`);
		}
		const entry = model === "*" ? `${provider}/*` : `${provider}/${model}`;
		if (seen.has(entry)) {
			return;
		}
		seen.add(entry);
		entries.push(entry);
		if (model === "*") {
			providers.add(provider);
		}
	});
	if (entries.length === 0) {
		throw new Error(`${source}: no allowed models configured; nothing may use the Telegram MCP. ${CREATE_HINT}`);
	}
	return { entries, providers };
}

function loadAllowList(): AllowList {
	const file = allowedModelsFile();
	let text: string;
	try {
		text = readFileSync(file, "utf8");
	} catch (error) {
		throw new Error(
			`cannot read the allowed-models file ${file}: ${(error as Error).message}. ${CREATE_HINT} ` +
				"Without it no model may use the Telegram MCP.",
		);
	}
	return parseAllowList(text, file);
}

function isAllowedModel(ctx: ExtensionContext | ExtensionToolContext, allowList: AllowList): boolean {
	const provider = ctx.model?.provider;
	const id = ctx.model?.id;
	if (!provider || !id) {
		return false;
	}
	return allowList.providers.has(provider) || allowList.entries.includes(`${provider}/${id}`);
}

function refreshedAllowList(): AllowList | null {
	try {
		return loadAllowList();
	} catch {
		return null;
	}
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

async function rpc(method: string, params: unknown, signal?: AbortSignal): Promise<any> {
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
			method,
			params,
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
	return payload?.result;
}

async function callMcp(name: string, args: unknown, signal?: AbortSignal): Promise<string> {
	const result = await rpc("tools/call", { name, arguments: args ?? {} }, signal);
	const text = resultText(result);
	if (result?.isError) {
		throw new Error(text || "the Telegram MCP tool failed");
	}
	return text;
}

// Which of the Saved Messages tools the server offers (MCP_SAVED_MESSAGES_WRITE=1); none on any error.
async function serverSavedWriteTools(): Promise<Set<string>> {
	try {
		const result = await rpc("tools/list", {}, AbortSignal.timeout(SERVER_LIST_TIMEOUT_MS));
		const names = new Set<string>((result?.tools ?? []).map((tool: any) => tool?.name));
		return new Set(SAVED_WRITE_SUFFIXES.filter((suffix) => names.has(suffix)));
	} catch {
		return new Set();
	}
}

export default function telegramMcpExtension(pi: ExtensionAPI): void {
	// The description reflects the list as loaded when the extension starts; the actual gate is
	// re-read from the file on every session_start/model_select and again at execute time.
	const initial = refreshedAllowList();
	const policy = initial ? accessPolicy(initial.entries.join(", ")) : noAllowListPolicy(allowedModelsFile());
	for (const tool of TOOLS) {
		pi.registerTool({
			name: `telegram_${tool.suffix}`,
			label: tool.label,
			description: `${policy} ${tool.summary}`,
			parameters: tool.parameters,
			defaultActive: false, // activated only for an allowed model (see syncActiveTools)
			annotations: {
				readOnlyHint: tool.readOnly,
				destructiveHint: false,
				idempotentHint: tool.readOnly,
				openWorldHint: true,
			},
			async execute(_toolCallId, params, signal, _onUpdate, ctx) {
				let allowList: AllowList;
				try {
					allowList = loadAllowList();
				} catch (error) {
					throw new Error((error as Error).message);
				}
				if (!isAllowedModel(ctx, allowList)) {
					throw new Error(REFUSAL);
				}
				const text = await callMcp(tool.suffix, params, signal);
				return { content: [{ type: "text", text }], details: undefined };
			},
		});
	}

	const syncActiveTools = async (ctx: ExtensionContext): Promise<void> => {
		const allowList = refreshedAllowList();
		const allowed = allowList !== null && isAllowedModel(ctx, allowList);
		// Only an allowed model makes the server round trip; the write tools fail closed.
		const offered = allowed ? await serverSavedWriteTools() : new Set<string>();
		const active = new Set(pi.getActiveTools());
		let changed = false;
		for (const tool of TOOLS) {
			const name = `telegram_${tool.suffix}`;
			const wanted = allowed && (!tool.savedWrite || offered.has(tool.suffix));
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
	pi.on("session_start", async (_event, ctx) => syncActiveTools(ctx));
	pi.on("model_select", async (_event, ctx) => syncActiveTools(ctx));
}
