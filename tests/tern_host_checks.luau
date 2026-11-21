-- Checks of tern/host.luau, appended to it by tests/test_tern_luau.py after
-- tests/tern_fake.luau: they see its top-level locals. Inside a function, as
-- the main chunk is at Luau's 200-local limit.

do (function()
	local function fresh(daemon: string?, skipped: string?)
		fake.reset()
		store.daemonId, store.daemonKnown, store.waitingSkipped = daemon, true, skipped
		store.waiting, store.waitingTicking, store.waitingAgain, store.waitingRecheck = nil, false, false, false
		store.paneStates, store.panesListed, store.waitingMemo = {}, false, nil
		store.jobEventAt, store.paneGoneAt, store.jobPending = {}, {}, {}
		store.scanning, store.scanRetryAt, store.waitingRaw = false, 0, ""
	end

	local function view(fields: any): any
		return { sessions = fields.sessions or {}, panes = fields.panes or {}, counts = {}, health = {} }
	end

	local function jobRow(pane: number, daemon: string?): any
		return { pane = pane, daemon = daemon, waits = { { kind = "job", state = "waiting", condition = { pane = pane } } } }
	end

	local function fakeBlock(): any
		local cx = { pane = 99, toasts = {} :: { any } }
		function cx.toast(self, ...)
			table.insert(self.toasts, { ... })
		end
		function cx.open() end
		function cx.render() end
		return { cx = cx, launched = {}, closed = false }
	end

	local function eventRuns(kind: string): { any }
		local out = {}
		for _, r in fake.runsOf("codexbar_waiting.py") do
			if fake.has(r.argv, "event") and fake.has(r.argv, kind) then
				table.insert(out, r)
			end
		end
		return out
	end

	check("ours: a row is this daemon's by its stamp, else while this daemon ticks", function()
		fresh("d1")
		assert(ours({ daemon = "d1" }), "stamped with our id")
		assert(not ours({ daemon = "d2" }), "stamped with another daemon's id")
		assert(ours({}), "unstamped while we tick")
		assert(not ours({ daemon = "d1" }, { daemon = "d2" }), "the wait's stamp wins over the row's")
		fresh("d1", "another daemon ticks")
		assert(not ours({}), "unstamped while another daemon ticks")
		assert(ours({ daemon = "d1" }), "stamped ours while another daemon ticks")
		fresh(nil)
		store.daemonKnown = false
		assert(not ours({ daemon = "d1" }), "a stamp counts as nobody's until the id is known")
	end)

	check("jobPanes: only this daemon's job waits", function()
		fresh("d1")
		local panes = jobPanes(view({ panes = { jobRow(3, "d2"), jobRow(4, "d1"), jobRow(5) } }))
		assert(not panes[3] and panes[4] and panes[5], "panes " .. tern.json.encode(panes))
	end)

	check("waitingPanes: leaves out another daemon's rows", function()
		fresh("d1")
		store.waiting = view({
			panes = { { pane = 3, daemon = "d2", status = "x" }, { pane = 4, daemon = "d1", status = "y" }, { pane = 5, status = "z" } },
			sessions = { { pane = 6, daemon = "d2", live = true, session_id = "s6" } },
		})
		local panes = waitingPanes()
		assert(panes["3"] == nil and panes["6"] == nil, "a foreign row's pane listed")
		assert(panes["4"] ~= nil and panes["5"] ~= nil, "our rows missing")
	end)

	check("cardPane: a foreign row has no card pane", function()
		fresh("d1")
		assert(cardPane({ pane = 4, daemon = "d1" }, true) == 4, "our pane row")
		assert(cardPane({ pane = 4, daemon = "d2" }, true) == nil, "a foreign pane row")
		assert(cardPane({ pane = 4, daemon = "d2", session_id = "s", live = true }, true) == nil, "a foreign session row")
	end)

	check("waitingGroups: a foreign row's state is open", function()
		fresh("d1")
		store.panesListed = true
		store.paneStates = { [4] = "running" }
		local mine = { pane = 4, ball = "on_me", daemon = "d1" }
		local foreign = { pane = 4, ball = "on_me", daemon = "d2" }
		local gone = { pane = 8, ball = "on_me", daemon = "d1" }
		store.waiting = view({ panes = { mine, foreign, gone } })
		local states = {}
		for _, g in waitingGroups("") do
			for _, r in g.rows do
				states[r.daemon .. ":" .. r.pane] = g.states[r]
			end
		end
		assert(states["d1:4"] == "running", "ours: " .. tostring(states["d1:4"]))
		assert(states["d2:4"] == "open", "foreign: " .. tostring(states["d2:4"]))
		assert(states["d1:8"] == "closed", "ours gone: " .. tostring(states["d1:8"]))
	end)

	check("waitingOpen: a foreign row targets none of this daemon's panes", function()
		fresh("d1")
		fake.panes = { { pane = 4 } }
		local b = fakeBlock()
		waitingOpen(b, { pane = 4, daemon = "d2", session_id = "s", live = true })
		assert(fake.kv.waitingRequest == nil, "requested " .. tern.json.encode(fake.kv.waitingRequest))
		assert(#fake.runsOf("codexbar_focus.py") == 1, "a foreign live session is focused where it runs")
		waitingOpen(b, { pane = 4, daemon = "d2" })
		assert(fake.kv.waitingRequest == nil, "requested a foreign pane row")
		assert(#b.cx.toasts == 1, "a foreign pane row says why nothing opens")
		waitingOpen(b, { pane = 4, daemon = "d1" })
		assert(fake.kv.waitingRequest and fake.kv.waitingRequest.target == 4, "our pane row opens")
	end)

	check("job_finished: resent from the tick until the CLI takes it", function()
		fresh("d1")
		fake.panes = { { pane = 7 } }
		store.waiting = view({ panes = { jobRow(7, "d1") } })
		fake.handlers.command_finished({ pane = 7, status = 3 })
		local sent = eventRuns("job_finished")
		assert(#sent == 1, "first send: " .. #sent)
		assert(fake.has(sent[1].argv, "3") and fake.has(sent[1].argv, "d1"), "args " .. table.concat(sent[1].argv, " "))
		reconcilePanes()
		assert(#eventRuns("job_finished") == 1, "no resend while one is in flight")
		assert(fake.answer(sent[1], { status = 1, stderr = "boom" }) == nil)
		reconcilePanes()
		sent = eventRuns("job_finished")
		assert(#sent == 2, "resend after a failure: " .. #sent)
		assert(fake.has(sent[2].argv, "3"), "resend keeps the status")
		assert(fake.answer(sent[2], { stdout = fake.json({ ok = true }) }) == nil)
		reconcilePanes()
		assert(#eventRuns("job_finished") == 2, "no resend after success")
	end)

	check("job_finished: a failed send is dropped once no wait watches the pane", function()
		fresh("d1")
		fake.panes = { { pane = 7 } }
		store.waiting = view({ panes = { jobRow(7, "d1") } })
		fake.handlers.command_finished({ pane = 7, status = 0 })
		assert(fake.answer(eventRuns("job_finished")[1], { status = 1 }) == nil)
		store.waiting = view({})
		reconcilePanes()
		store.waiting = view({ panes = { jobRow(7, "d1") } })
		reconcilePanes()
		assert(#eventRuns("job_finished") == 1, "resent after the wait was gone")
	end)

	check("measuredAt: the oldest of the enabled providers' newest readings", function()
		fresh("d1")
		fake.kv.settings = { providers = { "claude", "codex" } }
		local snap = { providers = {
			{ id = "claude", cachedAt = "2026-01-01T00:00:10Z" },
			{ id = "claude", cachedAt = "2026-01-01T00:00:30Z" },
			{ id = "codex", updatedAt = "2026-01-01T00:00:20Z" },
			{ id = "zai", cachedAt = "2026-01-01T00:05:00Z" },
		} }
		assert(measuredAt(snap, true) == epoch("2026-01-01T00:00:20Z"), "got " .. measuredAt(snap, true))
		assert(measuredAt(snap) == epoch("2026-01-01T00:05:00Z"), "the newest reading of any: " .. measuredAt(snap))
		table.remove(snap.providers, 3)
		assert(measuredAt(snap, true) == 0, "a provider without a reading counts as 0")
	end)

	check("scanAgents: no history limit, and a minute's wait after a failure", function()
		fresh("d1")
		scanAgents()
		local runs = fake.runsOf("codexbar_agents.py")
		assert(#runs == 1 and not fake.has(runs[1].argv, "--history-limit"), "argv " .. table.concat(runs[1].argv, " "))
		assert(fake.answer(runs[1], { status = 1, stderr = "boom" }) == nil)
		scanAgents()
		assert(#fake.runsOf("codexbar_agents.py") == 1, "rescanned right after a failure")
	end)

	check("waitingTick: the next tick is due even when applying the answer fails", function()
		fresh("d1")
		store.waitingAt, store.waitingAtMono, store.waitingSig = 0, 0, ""
		fake.failKv.waitingView = true
		waitingTick()
		local runs = fake.runsOf("codexbar_waiting.py")
		assert(#runs == 1, "ticks " .. #runs)
		local err = fake.answer(runs[1], { stdout = fake.json({ view = view({}), actions = {}, next_tick_s = 45 }) })
		assert(err == nil, "the callback raised: " .. tostring(err))
		assert(store.waitingAt >= os.time() + 40 and store.waitingAtMono >= os.clock() + 40, "next tick not scheduled")
		assert(not store.waitingTicking, "still ticking")
	end)

	check("waitingTick: a skipped tick publishes what acts on this daemon's panes", function()
		fresh("d1")
		store.waitingAt, store.waitingAtMono, store.waitingSig, store.waitingActionsSig = 0, 0, "", nil
		waitingTick()
		local runs = fake.runsOf("codexbar_waiting.py")
		local answer = {
			view = view({}), skipped = "another Tern daemon ticks this state", next_tick_s = 300,
			actions = { { id = "a1", type = "park", pane = 4 } },
		}
		assert(fake.answer(runs[1], { stdout = fake.json(answer) }) == nil)
		local published = fake.kv.waitingActions
		assert(type(published) == "table" and #published == 1 and published[1].id == "a1", tern.json.encode(published))
	end)

	check("tick: while skipped, an action on this daemon's pane missing from the outbox takes a tick", function()
		local function polled(published: { any }): number
			fresh("d1", "another Tern daemon ticks this state")
			store.waitingActions, store.waitingRaw, ticks = published, "1", 3
			store.waitingAt, store.waitingAtMono = math.huge, math.huge -- no timed tick due
			fake.files[STATE_DIR .. "/waiting.stamp"] = "2"
			fake.files[WAITING_FILE] = fake.json({ actions = {
				{ id = "a1", type = "park", pane = 4, daemon = "d1" },
				{ id = "a2", type = "park", pane = 4, daemon = "d2" },
				{ id = "a3", type = "toast" },
			} })
			tick()
			local runs = 0
			for _, r in fake.runsOf("codexbar_waiting.py") do
				runs += if fake.has(r.argv, "tick") then 1 else 0
			end
			return runs
		end
		assert(polled({}) == 1, "a1 queued since the outbox went out")
		assert(polled({ { id = "a1" } }) == 0, "the outbox is current")
		assert(polled({ { id = "a1" }, { id = "a0" } }) == 1, "a0 gone since the outbox went out")
	end)

	check("applyUsage: a failure without stderr still says something", function()
		fresh("d1")
		applyUsage({ status = 2, stdout = "", stderr = "", timed_out = false })
		assert(store.usageError == "exit 2", "got " .. tostring(store.usageError))
		applyUsage({ status = 0, stdout = "not json", stderr = "", timed_out = false })
		assert(store.usageError == "unreadable answer", "got " .. tostring(store.usageError))
		applyUsage({ status = 1, stdout = "", stderr = "boom\nmore", timed_out = false })
		assert(store.usageError == "boom", "got " .. tostring(store.usageError))
	end)

	check("prefs: the About page lists companions once the helper answers, read again on each open", function()
		fresh("d1")
		store.companionsLoading = false
		local s = prefs.new(fakeBlock().cx, "about")
		prefs.new(fakeBlock().cx, "about")
		local runs = fake.runsOf("codexbar_companions.py")
		assert(#runs == 1, "a second open while one runs starts no other: " .. #runs)
		local answer = { companions = {
			{ id = "tern-awui", name = "tern-awui", url = "https://example.com/awui#install", benefit = "On the phone.",
				installed = true, status = "Installed" },
			{ id = "omp-compact", name = "omp-compact", url = "https://example.com/compact#install", benefit = "Folds tools.",
				installed = false, status = "Not found" },
			{ id = "bad", name = "bad", url = "file:///etc/passwd", benefit = "", installed = false, status = "" },
		} }
		assert(fake.answer(runs[1], { stdout = fake.json(answer) }) == nil)
		local sections = settingsSections(s)
		local section = sections[#sections]
		assert(section.title == "Works well with" and #section.rows == 2, tern.json.encode(section))
		local awui, compact = section.rows[1], section.rows[2]
		assert(awui.hint == "Installed. On the phone." and awui.control.label == "Open GitHub", tern.json.encode(awui))
		assert(compact.hint == "Not found. Folds tools." and compact.control.label == "Install steps", tern.json.encode(compact))
		prefs.event(s, { ev = "action", act = "page", value = "about" })
		runs = fake.runsOf("codexbar_companions.py")
		assert(#runs == 2, "switching to About reads the status again")
		answer.companions[2].installed, answer.companions[2].status = true, "Installed"
		assert(fake.answer(runs[2], { stdout = fake.json(answer) }) == nil)
		sections = settingsSections(s)
		assert(#sections[#sections].rows == 2 and sections[#sections].rows[2].control.label == "Open GitHub", "rows updated in place")
		prefs.event(s, { ev = "action", act = "open_github", value = "companion.omp-compact" })
		local opened = fake.runsOf("https://example.com/compact#install")
		assert(#opened == 1 and opened[1].argv[1] == "xdg-open", "opens the install link")
	end)

	check("prefs: the update check fills About › Version and Update runs the helper, then reloads", function()
		fresh("d1")
		store.update, store.updateError, store.updateLoading, store.updateApplying, store.version = nil, nil, false, false, nil
		fake.files["/plugin/tern/plugin.toml"] = 'id = "codexbar-agentview"\nversion = "1.0.0"\n'
		local cx = fakeBlock().cx
		local copied = {}
		function cx.copy(_self, text: string)
			table.insert(copied, text)
		end
		local s = prefs.new(cx, "about")
		prefs.updateRows()
		local function versionRows(): { any }
			return settingsSections(s)[1].rows
		end
		assert(prefs.newerRelease() == nil, "nothing known yet")
		local rows = versionRows()
		assert(#rows == 1 and rows[1].label == "CodexBar AgentView 1.0.0" and rows[1].control == nil, tern.json.encode(rows))
		prefs.checkUpdate()
		prefs.checkUpdate()
		local runs = fake.runsOf("codexbar_update.py")
		assert(#runs == 1 and fake.has(runs[1].argv, "check"), "one check at a time")
		local answer = {
			current = "1.0.0", latest = "1.1.0", newer = true, url = "https://github.com/materemias/codexbar-agentview/releases/tag/v1.1.0",
			update = { possible = false, reason = "Branch master has commits v1.1.0 doesn't.", command = "git -C /x merge v1.1.0" },
		}
		assert(fake.answer(runs[1], { stdout = fake.json(answer) }) == nil)
		assert(prefs.newerRelease() == "1.1.0", "the footer's segment shows")
		rows = versionRows()
		assert(#rows == 3 and rows[1].hint == "1.1.0 is available.", tern.json.encode(rows))
		assert(rows[2].label == "Update to 1.1.0" and rows[2].control.label == "Copy command", tern.json.encode(rows[2]))
		assert(rows[3].control.label == "Open GitHub", tern.json.encode(rows[3]))
		-- Refused: activation copies the command and runs nothing.
		prefs.event(s, { ev = "action", act = "open_github", value = "update.apply" })
		assert(copied[1] == "git -C /x merge v1.1.0" and #fake.runsOf("codexbar_update.py") == 1, tern.json.encode(copied))
		-- Possible: `update --apply`, a toast, then a reload unless Tern's own came first.
		store.update.update = { possible = true, command = "git -C /x merge --ff-only v1.1.0" }
		prefs.updateRows()
		assert(versionRows()[2].control.label == "Update", "the button updates")
		assert(prefs.updateFocus() == "update.apply", "the footer opens on Update")
		prefs.event(s, { ev = "action", act = "open_github", value = "update.apply" })
		prefs.event(s, { ev = "action", act = "open_github", value = "update.apply" })
		runs = fake.runsOf("codexbar_update.py")
		assert(#runs == 2 and fake.has(runs[2].argv, "--apply"), "one apply at a time")
		local timers = {}
		local timer = tern.timer
		tern.timer = function(ms: number, fn: any)
			table.insert(timers, { ms = ms, fn = fn })
		end
		local err = fake.answer(runs[2], { stdout = fake.json({ ok = true, updated = true, from = "1.0.0", to = "1.1.0" }) })
		tern.timer = timer
		assert(err == nil, err)
		local toast = cx.toasts[#cx.toasts]
		assert(toast[1] == "success" and toast[2] == "Updated to 1.1.0", tern.json.encode(cx.toasts))
		local function reloads(): number
			local n = 0
			for _, r in fake.runsOf("codexbar_update.py") do
				n += if fake.has(r.argv, "reload") then 1 else 0
			end
			return n
		end
		runs = fake.runsOf("codexbar_update.py")
		assert(#runs == 3 and fake.has(runs[3].argv, "check"), "the rows check the updated checkout")
		assert(#timers == 1 and reloads() == 0, "the reload waits for Tern's own")
		timers[1].fn()
		assert(reloads() == 1, "reloads when Tern didn't")
		-- An apply that outlived its VM toasts from the new one; other children's ends don't.
		local count = #cx.toasts
		fake.handlers.process_exit({ status = 0, stdout = fake.json({ ok = true, view = {} }) }, cx)
		fake.handlers.process_exit({ status = 0, stdout = fake.json({ ok = true, updated = true, from = "1.0.0", to = "1.1.0" }) }, cx)
		assert(#cx.toasts == count + 1 and cx.toasts[#cx.toasts][2] == "Updated to 1.1.0", tern.json.encode(cx.toasts))
		-- Off: no check, the answer is forgotten and the helper's cache goes, for omp too.
		local cache = STATE_DIR .. "/update_check.json"
		fake.files[cache] = "{}"
		saveSetting(s, SETTINGS_BY_ID.updateCheck, false)
		assert(fake.files[cache] == nil, "the cache is removed")
		assert(prefs.newerRelease() == nil and versionRows()[1].hint == "The update check is off: General › Updates.", tern.json.encode(versionRows()))
		store.updateLoading = false
		prefs.checkUpdate()
		assert(#fake.runsOf("codexbar_update.py") == 4, "off runs no check")
		store.writeSetting("updateCheck", nil)
	end)

	fake.runChecks()
end)() end
