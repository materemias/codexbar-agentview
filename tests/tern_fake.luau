-- A fake `tern` for the plugin's Luau checks (tests/test_tern_luau.py). It
-- carries enough of the API for host.luau and window.luau to load; the checks
-- read what the plugin did through `fake`: process runs, kv, toasts, handlers.
-- Everything here sits in a `do` block: the plugin's main chunk, spliced in
-- after it, is at Luau's 200-local limit.

fake = {} :: any

do
	local NULL = setmetatable({}, { __tostring = function()
		return "null"
	end })
	-- What no check reads (ui nodes, commands, chrome): indexing, calling or
	-- assigning to it does nothing and gives itself.
	local any
	any = setmetatable({}, {
		__index = function()
			return any
		end,
		__call = function()
			return any
		end,
		__newindex = function() end,
	})
	local arrays = setmetatable({}, { __mode = "k" })

	local function copy(v: any, frozen: boolean?): any
		if type(v) ~= "table" or v == NULL or v == any then
			return v
		end
		local out = {}
		for k, x in v do
			out[k] = copy(x, frozen)
		end
		if arrays[v] then
			arrays[out] = true
		end
		return if frozen then table.freeze(out) else out
	end

	local function encode(v: any): string
		if v == nil or v == NULL then
			return "null"
		elseif type(v) == "boolean" or type(v) == "number" then
			return tostring(v)
		elseif type(v) == "string" then
			return string.format("%q", v)
		elseif type(v) ~= "table" then
			error("can't encode a " .. type(v))
		end
		local parts = {}
		if arrays[v] or #v > 0 or next(v) == nil then
			for _, x in v do
				table.insert(parts, encode(x))
			end
			return "[" .. table.concat(parts, ",") .. "]"
		end
		local entries = {}
		for k, x in v do
			table.insert(entries, { key = tostring(k), value = x })
		end
		table.sort(entries, function(a, b)
			return a.key < b.key
		end)
		for _, e in entries do
			table.insert(parts, string.format("%q", e.key) .. ":" .. encode(e.value))
		end
		return "{" .. table.concat(parts, ",") .. "}"
	end

	-- JSON text stands for a value through a token: fake.json(value) makes one,
	-- and decoding it gives a fresh copy of the value.
	local decoded: { [string]: any } = {}
	local tokens = 0
	function fake.json(v: any): string
		tokens += 1
		local token = "\0json" .. tokens
		decoded[token] = copy(v)
		return token
	end

	fake.NULL = NULL
	fake.any = any
	-- The window half gets kv tables frozen, as Tern protects them there.
	fake.frozenKv = false
	function fake.reset()
		fake.kv = {}
		fake.failKv = {} -- key -> true: setting it raises
		fake.files = {}
		fake.runs = {}
		fake.panes = {}
		fake.logs = {}
	end
	fake.reset()
	fake.handlers = {}

	-- The runs whose argv names `script`, oldest first.
	function fake.runsOf(script: string): { any }
		local out = {}
		for _, r in fake.runs do
			for _, a in r.argv do
				if type(a) == "string" and string.sub(a, -#script) == script then
					table.insert(out, r)
					break
				end
			end
		end
		return out
	end

	-- Answers a run as a finished process would; the error its callback raised, if any.
	function fake.answer(run: any, res: any): string?
		assert(not run.answered, "run answered twice")
		run.answered = true
		local full = { status = res.status or 0, stdout = res.stdout or "", stderr = res.stderr or "", timed_out = res.timed_out or false }
		local ok, err = pcall(run.cb, full, run.cx)
		return if ok then nil else tostring(err)
	end

	function fake.has(list: { any }, value: any): boolean
		return table.find(list, value) ~= nil
	end

	local function log(...)
		table.insert(fake.logs, table.concat(table.pack(...) :: any, " ", 1, select("#", ...)))
	end

	tern = setmetatable({
		runtime = { os = "linux" },
		getenv = function(name: string): string?
			return ({ HOME = "/home/test", XDG_STATE_HOME = "/state", PATH = "/usr/bin", SHELL = "/bin/sh" })[name]
		end,
		plugin = { dir = "/plugin/tern", data = "/plugin-data/codexbar-agentview", id = "codexbar-agentview" },
		fs = {
			read = function(path: string): string
				local text = fake.files[path]
				if text == nil then
					error("no such file: " .. path)
				end
				return text
			end,
			exists = function(path: string): boolean
				return fake.files[path] ~= nil
			end,
		},
		json = {
			null = NULL,
			array = function(t: any?): any
				local out = t or {}
				arrays[out] = true
				return out
			end,
			encode = encode,
			decode = function(text: string): any
				local v = decoded[text]
				if v == nil then
					error("not JSON")
				end
				return copy(v)
			end,
		},
		kv = {
			get = function(key: string): any
				return copy(fake.kv[key], fake.frozenKv)
			end,
			set = function(key: string, value: any)
				if fake.failKv[key] then
					error("kv set failed: " .. key)
				end
				fake.kv[key] = copy(value)
			end,
		},
		log = { warn = log, error = log, info = log },
		timer = function() end,
		on = function(name: string, fn: any)
			fake.handlers[name] = fn
		end,
		process = {
			run = function(argv: { string }, _opts: any, cb: any)
				table.insert(fake.runs, { argv = argv, cb = cb })
			end,
		},
		pane = {
			list = function(): { any }
				return fake.panes
			end,
		},
		now = function(): number
			return 0
		end,
		ui = any,
	}, {
		__index = function()
			return any
		end,
	})

	-- Runs every check in order, each in its own pcall; prints one line per check.
	local checks: { { name: string, fn: () -> () } } = {}
	function check(name: string, fn: () -> ())
		table.insert(checks, { name = name, fn = fn })
	end
	function fake.runChecks()
		for _, c in checks do
			local ok, err = pcall(c.fn)
			print((if ok then "ok " else "FAIL ") .. c.name .. (if ok then "" else " :: " .. tostring(err)))
		end
	end
end
