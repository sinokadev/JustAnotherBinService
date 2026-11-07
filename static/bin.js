(() => {
    const page = document.querySelector(".bin-view");
    if (!page) return;

    const hexOutput = document.querySelector("#hex-output");
    const hexScroll = document.querySelector("#hex-scroll");
    const hexStatus = document.querySelector("#hex-status");
    const isLocal = page.dataset.local === "true";
    const mode = page.dataset.mode;

    let viewerBytes = new Uint8Array();
    let bytesPerRow = 16;
    let searchOffsets = [];
    let currentSearchIndex = -1;
    const rowHeight = 26;

    function formatSize(size) {
        if (size < 1024) return `${size} bytes`;
        const units = ["KiB", "MiB", "GiB", "TiB"];
        let i = -1;
        while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
        return `${size.toFixed(2)} ${units[i]}`;
    }

    function updateDetails(bytes, chars) {
        const sh = document.querySelector("#size-human");
        const sb = document.querySelector("#size-bytes");
        const sc = document.querySelector("#size-chars");
        if (sh) sh.textContent = formatSize(bytes.byteLength);
        if (sb) sb.textContent = bytes.byteLength.toLocaleString();
        if (sc) sc.textContent = chars.toLocaleString();
    }

    function renderHex() {
        if (!hexOutput || !hexScroll) return;
        const totalRows = Math.ceil(viewerBytes.length / bytesPerRow);
        const topRow = Math.max(0, Math.floor(hexScroll.scrollTop / rowHeight) - 8);
        const count = Math.ceil(hexScroll.clientHeight / rowHeight) + 16;
        const bottomRow = Math.min(totalRows, topRow + count);
        const matchRow = searchOffsets[currentSearchIndex] !== undefined
            ? Math.floor(searchOffsets[currentSearchIndex] / bytesPerRow) : -1;

        const frag = document.createDocumentFragment();
        const topSpacer = document.createElement("div");
        topSpacer.className = "hex-spacer";
        topSpacer.style.height = `${topRow * rowHeight}px`;
        frag.appendChild(topSpacer);

        for (let r = topRow; r < bottomRow; r++) {
            const offset = r * bytesPerRow;
            const chunk = viewerBytes.subarray(offset, Math.min(offset + bytesPerRow, viewerBytes.length));
            const hexParts = [];
            let asciiStr = "";
            for (let i = 0; i < chunk.length; i++) {
                const b = chunk[i];
                hexParts.push((i > 0 && i % 8 === 0 ? " " : "") + b.toString(16).padStart(2, "0"));
                asciiStr += (b >= 32 && b <= 126) ? String.fromCharCode(b) : ".";
            }

            const row = document.createElement("div");
            row.className = "hex-row" + (r === matchRow ? " is-match" : "");
            const addrHex = offset.toString(16).padStart(8, "0");
            row.innerHTML = `<span class="hex-offset">${addrHex}</span><span class="hex-bytes">${hexParts.join(" ")}</span><span class="hex-ascii">${asciiStr.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</span><button type="button" class="hex-copy" title="Copy row as hex" aria-label="Copy bytes at offset ${addrHex}">⧉</button>`;

            row.querySelector(".hex-copy").addEventListener("click", async () => {
                const hexStr = Array.from(chunk, b => b.toString(16).padStart(2, "0")).join(" ");
                try {
                    await navigator.clipboard.writeText(hexStr);
                    if (hexStatus) hexStatus.textContent = `Copied bytes at offset ${addrHex}.`;
                } catch {
                    if (hexStatus) hexStatus.textContent = "Clipboard access unavailable.";
                }
            });
            frag.appendChild(row);
        }

        const bottomSpacer = document.createElement("div");
        bottomSpacer.className = "hex-spacer";
        bottomSpacer.style.height = `${(totalRows - bottomRow) * rowHeight}px`;
        frag.appendChild(bottomSpacer);

        hexOutput.replaceChildren(frag);
    }

    function jumpToOffset(offset) {
        if (!hexScroll) return;
        const bounded = Math.max(0, Math.min(offset, viewerBytes.length - 1));
        hexScroll.scrollTop = Math.floor(bounded / bytesPerRow) * rowHeight;
        renderHex();
    }

    function findMatches(pattern) {
        const matches = [];
        const plen = pattern.length;
        const vlen = viewerBytes.length;
        if (plen === 0 || vlen < plen) return matches;
        for (let i = 0; i <= vlen - plen && matches.length < 1000; i++) {
            let match = true;
            for (let j = 0; j < plen; j++) {
                if (viewerBytes[i + j] !== pattern[j]) { match = false; break; }
            }
            if (match) matches.push(i);
        }
        return matches;
    }

    function goToMatch(index) {
        if (!searchOffsets.length) return;
        currentSearchIndex = (index + searchOffsets.length) % searchOffsets.length;
        const offset = searchOffsets[currentSearchIndex];
        jumpToOffset(offset);
        if (hexStatus) hexStatus.textContent = `Match ${currentSearchIndex + 1} of ${searchOffsets.length} at 0x${offset.toString(16)}.`;
    }

    function initHexViewer(bytes) {
        if (!hexOutput) return;
        viewerBytes = bytes;
        renderHex();
        hexScroll?.addEventListener("scroll", renderHex, { passive: true });

        document.querySelector("#hex-width")?.addEventListener("change", e => {
            const oldOffset = Math.floor(hexScroll.scrollTop / rowHeight) * bytesPerRow;
            bytesPerRow = Number(e.target.value);
            hexScroll.scrollTop = Math.floor(oldOffset / bytesPerRow) * rowHeight;
            renderHex();
        });

        document.querySelector("#hex-search-form")?.addEventListener("submit", e => {
            e.preventDefault();
            const val = document.querySelector("#hex-search").value.trim().replace(/^0x/i, "").replace(/\s/g, "");
            if (!val || val.length % 2 !== 0 || !/^[0-9a-f]+$/i.test(val)) {
                if (hexStatus) hexStatus.textContent = "Enter an even number of hex digits (e.g. 48 65 6c).";
                return;
            }
            const pattern = new Uint8Array(val.match(/.{2}/g).map(p => parseInt(p, 16)));
            searchOffsets = findMatches(pattern);
            currentSearchIndex = -1;
            const prev = document.querySelector("#hex-previous");
            const next = document.querySelector("#hex-next");
            if (prev) prev.disabled = !searchOffsets.length;
            if (next) next.disabled = !searchOffsets.length;
            if (!searchOffsets.length) {
                if (hexStatus) hexStatus.textContent = "No matches found.";
                renderHex();
                return;
            }
            if (searchOffsets.length === 1000 && hexStatus) hexStatus.textContent = "Showing first 1,000 matches.";
            goToMatch(0);
        });

        document.querySelector("#hex-previous")?.addEventListener("click", () => goToMatch(currentSearchIndex - 1));
        document.querySelector("#hex-next")?.addEventListener("click", () => goToMatch(currentSearchIndex + 1));

        document.querySelector("#hex-goto-form")?.addEventListener("submit", e => {
            e.preventDefault();
            const val = document.querySelector("#hex-goto").value.trim().replace(/^0x/i, "");
            if (!/^[0-9a-f]+$/i.test(val)) {
                if (hexStatus) hexStatus.textContent = "Enter a hexadecimal offset, such as 1a0.";
                return;
            }
            const offset = parseInt(val, 16);
            if (offset >= viewerBytes.length) {
                if (hexStatus) hexStatus.textContent = "Offset is past the end of this Bin.";
                return;
            }
            jumpToOffset(offset);
            if (hexStatus) hexStatus.textContent = `Showing offset 0x${offset.toString(16)}.`;
        });
    }

    function openLocalDatabase(onSuccess, onError) {
        const req = indexedDB.open("bin-sinoka-local", 1);
        req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains("bins")) db.createObjectStore("bins");
        };
        req.onerror = onError;
        req.onsuccess = () => onSuccess(req.result);
    }

    function showBase64Action(source) {
        const btn = document.querySelector("#decode-base64");
        const compact = source.replace(/\s/g, "").replace(/-/g, "+").replace(/_/g, "/");
        if (!btn || !compact || compact.length % 4 === 1) return;
        const padded = compact + "=".repeat((4 - compact.length % 4) % 4);
        if (!/^[A-Za-z0-9+/]*={0,2}$/.test(padded)) return;
        try {
            const decoded = atob(padded);
            if (btoa(decoded).replace(/=+$/, "") !== compact.replace(/=+$/, "")) return;
            btn.hidden = false;
            btn.addEventListener("click", e => {
                e.preventDefault();
                saveDecodedBytes(decoded);
            });
        } catch {}
    }

    function saveDecodedBytes(decoded) {
        const bytes = Uint8Array.from(decoded, c => c.charCodeAt(0));
        openLocalDatabase(db => {
            const tx = db.transaction("bins", "readwrite");
            tx.objectStore("bins").put({ bytes: bytes.buffer, savedAt: new Date().toISOString() }, "current");
            tx.oncomplete = () => location.assign(page.dataset.localUrl);
            tx.onerror = () => alert("로컬 Bin 저장 실패");
        }, () => alert("브라우저 저장소 접근 실패"));
    }

    function loadLocalContent() {
        const status = document.querySelector("#local-status");
        openLocalDatabase(db => {
            const tx = db.transaction("bins", "readonly");
            const req = tx.objectStore("bins").get("current");
            req.onsuccess = () => {
                if (!req.result) {
                    if (status) status.textContent = "저장된 로컬 Bin이 없습니다.";
                    return;
                }
                const bytes = new Uint8Array(req.result.bytes);
                const text = new TextDecoder().decode(bytes);
                status?.remove();
                updateDetails(bytes, text.length);
                if (mode === "hex") {
                    initHexViewer(bytes);
                } else {
                    const el = document.querySelector("#local-content");
                    if (el) el.textContent = text;
                }
            };
            req.onerror = () => { if (status) status.textContent = "로컬 Bin 읽기 실패"; };
        }, () => { if (status) status.textContent = "브라우저 저장소 열기 실패"; });
    }

    if (isLocal) {
        loadLocalContent();
        return;
    }

    const contentEl = document.querySelector("#bin-content");
    if (contentEl) {
        const source = contentEl.textContent;
        const bytes = new TextEncoder().encode(source);
        if (mode === "hex") initHexViewer(bytes);
        showBase64Action(source);
    }
})();
