/**
 * Test-only pi extension: dump the active tool set and shut down.
 *
 * Used by integrations/pi/tests/verify.sh to check the model gate without calling a model.
 * Set PI_TEST_TOOLS_FILE to the output path.
 */

import { writeFileSync } from "node:fs";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

export default function activeToolsProbe(pi: ExtensionAPI): void {
	const record = (ctx: ExtensionContext): void => {
		const path = process.env.PI_TEST_TOOLS_FILE;
		if (!path) {
			return;
		}
		writeFileSync(
			path,
			`${JSON.stringify(
				{
					model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : null,
					active: pi.getActiveTools(),
				},
				null,
				2,
			)}\n`,
		);
		ctx.shutdown();
	};

	let timer: ReturnType<typeof setTimeout> | undefined;
	pi.on("session_start", (_event, ctx) => {
		timer = setTimeout(() => record(ctx), 500);
	});
	pi.on("model_select", (_event, ctx) => {
		if (timer) {
			clearTimeout(timer);
			timer = undefined;
		}
		record(ctx);
	});
}
