.pragma library

function shellQuote(value) {
    return "'" + String(value).replace(/'/g, "'\\''") + "'"
}

function localPath(url) {
    return decodeURIComponent(url.toString().replace(/^file:\/\//, ""))
}

function cliPath(configured) {
    return configured || "/usr/bin/codexbar"
}

// URL of one of our state files, from QtCore's GenericStateLocation (a QUrl;
// its escaping stays intact for XHR).
function stateFileUrl(stateLocation, name) {
    return stateLocation.toString().replace(/\/$/, "") + "/codexbar-agentview/" + name
}
