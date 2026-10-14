// Passkey (WebAuthn) support for Meetinghouse.
//
// The server sends options as JSON with binary fields in base64url; the
// browser API wants ArrayBuffers, and its answers come back as
// ArrayBuffers that we turn into base64url JSON for the server to verify.

(function () {
  function toBuf(b64url) {
    var s = b64url.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function toB64url(buf) {
    var bytes = new Uint8Array(buf), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function postJSON(url, body) {
    var resp = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    var data = {};
    try { data = await resp.json(); } catch (e) {}
    if (!resp.ok) throw new Error(data.error || "Something went wrong (" + resp.status + ").");
    return data;
  }

  function friendly(err) {
    if (err && err.name === "NotAllowedError") return "The passkey request was cancelled or timed out. Please try again.";
    if (err && err.name === "InvalidStateError") return "This device already has a passkey for your account.";
    return (err && err.message) || String(err);
  }

  function supported() {
    return !!(window.PublicKeyCredential && navigator.credentials);
  }

  async function addPasskey(button) {
    var status = document.getElementById(button.dataset.status);
    var label = document.getElementById(button.dataset.label);
    status.textContent = "Follow the prompt from your browser or phone…";
    button.disabled = true;
    try {
      var opts = await postJSON(button.dataset.options);
      opts.challenge = toBuf(opts.challenge);
      opts.user.id = toBuf(opts.user.id);
      (opts.excludeCredentials || []).forEach(function (c) { c.id = toBuf(c.id); });
      var cred = await navigator.credentials.create({ publicKey: opts });
      var result = await postJSON(button.dataset.verify, {
        label: label ? label.value : "",
        credential: {
          id: cred.id,
          rawId: toB64url(cred.rawId),
          type: cred.type,
          response: {
            clientDataJSON: toB64url(cred.response.clientDataJSON),
            attestationObject: toB64url(cred.response.attestationObject),
            transports: cred.response.getTransports ? cred.response.getTransports() : [],
          },
          clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
          authenticatorAttachment: cred.authenticatorAttachment || undefined,
        },
      });
      window.location = result.redirect;
    } catch (err) {
      status.textContent = friendly(err);
      button.disabled = false;
    }
  }

  async function logIn(button) {
    var status = document.getElementById(button.dataset.status);
    status.textContent = "Follow the prompt from your browser or phone…";
    button.disabled = true;
    try {
      var opts = await postJSON(button.dataset.options);
      opts.challenge = toBuf(opts.challenge);
      (opts.allowCredentials || []).forEach(function (c) { c.id = toBuf(c.id); });
      var cred = await navigator.credentials.get({ publicKey: opts });
      var result = await postJSON(button.dataset.verify, {
        credential: {
          id: cred.id,
          rawId: toB64url(cred.rawId),
          type: cred.type,
          response: {
            clientDataJSON: toB64url(cred.response.clientDataJSON),
            authenticatorData: toB64url(cred.response.authenticatorData),
            signature: toB64url(cred.response.signature),
            userHandle: cred.response.userHandle ? toB64url(cred.response.userHandle) : null,
          },
          clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
          authenticatorAttachment: cred.authenticatorAttachment || undefined,
        },
      });
      window.location = result.redirect;
    } catch (err) {
      status.textContent = friendly(err);
      button.disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-passkey]").forEach(function (button) {
      if (!supported()) {
        var status = document.getElementById(button.dataset.status);
        if (status) status.textContent = "This browser doesn't support passkeys. Try an up-to-date Safari, Chrome, Edge or Firefox.";
        button.disabled = true;
        return;
      }
      button.addEventListener("click", function () {
        if (button.dataset.passkey === "add") addPasskey(button);
        else logIn(button);
      });
    });
  });
})();
