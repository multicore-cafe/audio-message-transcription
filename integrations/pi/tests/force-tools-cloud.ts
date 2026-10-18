/**
 * Test-only pi extension: a fake "cloud" provider plus forced activation of one Telegram tool.
 *
 * Proves the extension's execution-time model check: even if the Telegram tool is somehow
 * active under a non-local model, the call is refused. The fake OpenAI-compatible backend
 * (fake_openai_server.py) returns a tool call, so no cloud model is involved.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const FORCED = "telegram_list_dialogs";

export default function forceToolsExtension(pi: ExtensionAPI): void {
	const baseUrl = process.env.PI_TEST_OPENAI_URL;
	if (baseUrl) {
		pi.registerProvider("local-check", {
			baseUrl,
			apiKey: "test",
			api: "openai-completions",
			models: [{
				id: "cloud-proxy",
				name: "Fake cloud identity (test)",
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 128000,
				maxTokens: 4096,
			}],
		});
	}
	// Discovered extensions may register after this one; force the tool right before the model call.
	for (const event of ["session_start", "before_agent_start"] as const) {
		pi.on(event, () => {
			pi.setActiveTools([...pi.getActiveTools(), FORCED]);
		});
	}
}
