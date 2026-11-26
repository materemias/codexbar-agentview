// Preloaded under bun: the host modules omp serves extensions, which plain bun cannot resolve.
Bun.plugin({
	name: "omp-host-stub",
	setup(build) {
		build.module("@oh-my-pi/pi-tui", () => ({ loader: "object", exports: { jsx: (kind, props) => ({ kind, props }), nativeRendering: () => false } }));
	},
});
