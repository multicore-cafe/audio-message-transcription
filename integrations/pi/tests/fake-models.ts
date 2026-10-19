/**
 * Test-only pi extension: fake providers for verify.sh's provider/model gate probes.
 *
 * Registers providers whose model ids exercise the exact-match, `/`-in-id and provider/* rules.
 * The active-tools probe never calls a model, and nothing here talks to a network.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function model(id: string) {
	return {
		id,
		name: id,
		reasoning: false,
		input: ["text"] as const,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 128000,
		maxTokens: 4096,
	};
}

function provider(ids: string[]) {
	return {
		baseUrl: "http://127.0.0.1:1/v1",
		apiKey: "test",
		api: "openai-completions" as const,
		models: ids.map(model),
	};
}

export default function fakeModels(pi: ExtensionAPI): void {
	pi.registerProvider("oneprovider", provider(["my-model"]));
	pi.registerProvider("someprovider", provider(["team/turbo", "team/other"]));
	pi.registerProvider("otherprovider", provider(["anything"]));
	pi.registerProvider("cloudprovider", provider(["kimi"]));
}
