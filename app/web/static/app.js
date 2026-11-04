(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = {
    tags: [], source: null, result: null, posts: [], users: [], loggedIn: false,
    engagement: { posts: {}, people: [], meta: null },  // posts keyed by shortcode
    roleFilter: "all",
    profiles: {},              // username -> profile, from the Profile button or the agent
    openProfiles: new Set(),   // usernames whose profile card is expanded
    loadingProfiles: new Set(),
    chat: [],           // {role, content} turns replayed to the agent
    agent: null,        // AbortController while the agent is answering
    agentReady: false,  // an LLM is configured on the server
    mapRun: null,       // AbortController while the mapping agent works
  };

  const ICON = {
    heart: '<svg width="20" height="20" viewBox="0 0 24 24" fill="#fff"><path d="M12 21s-7.5-4.6-9.5-9C1 8.2 3 5 6.5 5c2 0 3.5 1 5.5 3 2-2 3.5-3 5.5-3C21 5 23 8.2 21.5 12c-2 4.4-9.5 9-9.5 9z"/></svg>',
    comment: '<svg width="20" height="20" viewBox="0 0 24 24" fill="#fff"><path d="M12 3C6.5 3 2 6.8 2 11.5c0 2.4 1.2 4.6 3.2 6.1L4 21l4.3-1.6c1.1.3 2.4.5 3.7.5 5.5 0 10-3.8 10-8.5S17.5 3 12 3z"/></svg>',
    carousel: '<svg width="20" height="20" viewBox="0 0 24 24" fill="#fff"><path d="M8 4h12a2 2 0 0 1 2 2v12h-2V6H8V4zm-4 4h12a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2z"/></svg>',
    video: '<svg width="20" height="20" viewBox="0 0 24 24" fill="#fff"><path d="M6 4l14 8-14 8z"/></svg>',
    verified: '<svg class="icon-sm" viewBox="0 0 24 24" aria-label="Verified"><circle cx="12" cy="12" r="12" fill="#0095f6"/><path d="M7 12.5l3 3 7-7" stroke="#fff" stroke-width="2.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    lock: '<svg class="icon-sm" viewBox="0 0 24 24" fill="#8e8e8e" aria-label="Private"><path d="M7 10V7a5 5 0 0 1 10 0v3h1a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h1zm2 0h6V7a3 3 0 0 0-6 0v3z"/></svg>',
  };

  // ---- helpers -------------------------------------------------------------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n) => {
    if (n == null) return "–";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
    return String(n);
  };
  const date = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const proxy = (url) => `/api/image?url=${encodeURIComponent(url)}`;
  const profileUrl = (username) => `https://www.instagram.com/${encodeURIComponent(username || "")}/`;
  const img = (url, extra = "") => url
    ? `<img src="${esc(url)}" data-proxy="${esc(proxy(url))}" alt="" loading="lazy" ${extra}>`
    : "";
  // Instagram CDN images sometimes refuse hot-linking: retry through the local proxy, then give up gracefully.
  function wireImages(root) {
    root.querySelectorAll("img[data-proxy]").forEach((el) => {
      el.onerror = () => {
        if (!el.dataset.tried) { el.dataset.tried = "1"; el.src = el.dataset.proxy; return; }
        const avatar = el.closest(".avatar");
        if (avatar) avatar.innerHTML = `<div>${esc(avatar.dataset.initial || "?")}</div>`;
        else el.closest(".tile")?.classList.add("noimg");
      };
    });
  }
  function avatar(username, picUrl) {
    const initial = (username || "?")[0].toUpperCase();
    const pic = picUrl ? `<img src="${esc(proxy(picUrl))}" data-proxy="${esc(proxy(picUrl))}" data-tried="1" alt="" loading="lazy">` : `<div>${esc(initial)}</div>`;
    return `<div class="avatar" data-initial="${esc(initial)}">${pic}</div>`;
  }
  function showError(msg) { const el = $("error"); el.textContent = msg; el.classList.remove("hidden"); }
  function hideError() { $("error").classList.add("hidden"); }
  function flash(btn, text) { const old = btn.textContent; btn.textContent = text; setTimeout(() => { btn.textContent = old; }, 1200); }

  // ---- session ---------------------------------------------------------------
  async function checkSession() {
    const el = $("session"), text = $("sessionText");
    try {
      const s = await (await fetch("/api/status")).json();
      el.classList.toggle("ok", s.logged_in);
      el.classList.toggle("bad", !s.logged_in);
      text.textContent = s.logged_in ? `Logged in · user ${s.user_id}` : "Not logged in";
      if (!s.logged_in) showError(s.message || "No Instagram session. Run the login first.");
      setAgentInfo(s.agent);
      return s.logged_in;
    } catch {
      el.classList.add("bad");
      text.textContent = "Server unreachable";
      setAgentInfo(null);
      return false;
    }
  }

  // ---- hashtag chips -----------------------------------------------------------
  const chips = $("chips"), input = $("tagInput");
  function addTags(raw) {
    String(raw).split(/[\s,]+/)
      .map((t) => t.replace(/^#+/, "").trim().toLowerCase())
      .filter(Boolean)
      .forEach((t) => { if (!state.tags.includes(t)) state.tags.push(t); });
    renderChips();
  }
  function renderChips() {
    chips.querySelectorAll(".chip").forEach((c) => c.remove());
    state.tags.forEach((t) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.innerHTML = `#${esc(t)} <button type="button" aria-label="Remove #${esc(t)}">×</button>`;
      chip.querySelector("button").onclick = () => { state.tags = state.tags.filter((x) => x !== t); renderChips(); };
      chips.insertBefore(chip, input);
    });
    updateButtons();
  }
  function flushInput() { if (input.value.trim()) { addTags(input.value); input.value = ""; } }
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); const had = input.value.trim(); flushInput(); if (!had && state.tags.length) startScrape(); }
    else if (e.key === "," || e.key === " ") { e.preventDefault(); flushInput(); }
    else if (e.key === "Backspace" && !input.value && state.tags.length) { state.tags.pop(); renderChips(); }
  });
  input.addEventListener("paste", (e) => { e.preventDefault(); addTags(e.clipboardData.getData("text")); });
  input.addEventListener("blur", flushInput);
  chips.addEventListener("click", () => input.focus());
  function updateButtons() {
    // one browser tab: crawls, engagement, the agent and the mapping agent take turns
    const busy = !!state.source || !!state.agent || !!state.mapRun;
    $("go").disabled = !state.loggedIn || !state.tags.length || busy;
    $("engGo").disabled = !state.loggedIn || !state.posts.length || busy;
    $("ask").disabled = !state.loggedIn || !state.agentReady || busy || !$("question").value.trim();
    $("mapGo").disabled = !state.loggedIn || !state.agentReady || busy || !$("mapSeeds").value.trim();
  }

  // ---- progress + SSE plumbing ---------------------------------------------------
  function showProgress(title, ring, pills) {
    hideError();
    $("progress").classList.remove("hidden");
    $("ptitle").textContent = title;
    $("pmeta").textContent = "";
    $("ringTag").textContent = ring;
    $("tagProgress").innerHTML = pills || "";
    $("tagProgress").classList.toggle("hidden", !pills);
  }
  function finish() {
    if (state.source) { state.source.close(); state.source = null; }
    $("progress").classList.add("hidden");
    updateButtons();
  }
  function listen(url, handlers) {
    const es = new EventSource(url);
    state.source = es;
    updateButtons();
    Object.entries(handlers).forEach(([name, fn]) => es.addEventListener(name, (e) => fn(JSON.parse(e.data))));
    es.addEventListener("failed", (e) => { const d = JSON.parse(e.data); finish(); showError(`${d.kind}: ${d.message}`); });
    es.onerror = () => { if (state.source === es) { finish(); showError("Lost the connection to the local server."); } };
  }
  $("cancel").onclick = () => { finish(); showError("Stopped. The server finishes the current step and then idles."); };

  // ---- hashtag scrape --------------------------------------------------------------
  function startScrape() {
    flushInput();
    if (!state.tags.length || state.source) return;
    const params = new URLSearchParams({
      tags: state.tags.join(","),
      max_posts: $("maxPosts").value || "60",
      max_scrolls: $("maxScrolls").value || "5",
    });
    $("results").classList.add("hidden");
    showProgress("Opening Instagram…", "#", state.tags.map((t) => `<span class="tagpill" data-tag="${esc(t)}">#${esc(t)}</span>`).join(""));
    listen(`/api/scrape?${params}`, {
      tag_start: (d) => {
        $("ringTag").textContent = "#" + d.tag;
        $("ptitle").textContent = `Loading #${d.tag} (${d.index + 1}/${d.total})`;
        $("pmeta").textContent = "waiting for the first posts…";
        pill(d.tag, "active");
      },
      progress: (d) => { $("pmeta").textContent = `${plural(d.posts, "post")} · ${plural(d.users, "account")} · ${plural(d.scrolls, "scroll")}`; },
      tag_done: (d) => pill(d.tag, "done", `#${d.tag} · ${plural(d.post_count, "post")} · ${plural(d.user_count, "account")}`),
      done: (d) => { finish(); renderScrape(d); },
    });
  }
  function pill(tag, cls, text) {
    const p = $("tagProgress").querySelector(`[data-tag="${CSS.escape(tag)}"]`);
    if (!p) return;
    p.className = "tagpill " + cls;
    if (text) p.textContent = text;
  }
  $("go").onclick = startScrape;

  function renderScrape(result) {
    state.result = result;
    state.posts = result.posts || [];
    state.users = result.users || [];
    state.engagement = { posts: {}, people: [], meta: null };
    document.querySelectorAll(".stat.eng").forEach((el) => el.classList.add("hidden"));
    $("engStatus").textContent = "";
    syncEngagementControls();
    renderTagSummary();
    renderAll();
    updateButtons();  // the engagement button depends on there being posts
    $("results").classList.remove("hidden");
    showTab("posts");
    $("results").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function syncEngagementControls() {
    $("engPosts").max = String(Math.min(100, Math.max(1, state.posts.length)));
    $("engPosts").value = String(Math.min(30, state.posts.length || 1));
  }
  function renderTagSummary() {
    const tags = (state.result && state.result.tags) || [];
    $("sTags").textContent = tags.length;
    $("tagSummary").innerHTML = tags.map((t) =>
      `<span class="tagpill">#${esc(t.tag)} · ${plural(t.post_count, "post")} · ${plural(t.user_count, "account")}` +
      `${t.has_more ? " · more available" : ""}${t.message ? " · " + esc(t.message) : ""}</span>`).join("");
  }
  // A hashtag crawl the agent ran: first one replaces the page, later ones merge into it.
  function absorbScrape(result) {
    if (!result || !Array.isArray(result.tags)) return;
    if (!state.result) { renderScrape(result); return; }
    const have = new Set(state.posts.map((p) => p.shortcode));
    (result.posts || []).forEach((p) => { if (p.shortcode && !have.has(p.shortcode)) { state.posts.push(p); have.add(p.shortcode); } });
    const users = new Map(state.users.map((u) => [u.username, u]));
    (result.users || []).forEach((u) => {
      const row = users.get(u.username);
      if (!row) { users.set(u.username, { ...u, hashtags: [...(u.hashtags || [])] }); return; }
      row.post_count = (row.post_count || 0) + (u.post_count || 0);
      row.hashtags = row.hashtags || [];
      (u.hashtags || []).forEach((t) => { if (!row.hashtags.includes(t)) row.hashtags.push(t); });
    });
    state.users = [...users.values()].sort((a, b) => (b.hashtags || []).length - (a.hashtags || []).length || b.post_count - a.post_count);
    const seen = new Set((state.result.tags || []).map((t) => t.tag));
    state.result = {
      ...state.result,
      tags: [...(state.result.tags || []), ...result.tags.filter((t) => !seen.has(t.tag))],
      posts: state.posts, users: state.users,
      post_count: state.posts.length, user_count: state.users.length, usernames: state.users.map((u) => u.username),
    };
    syncEngagementControls();
    renderTagSummary();
    renderAll();
    updateButtons();
    $("results").classList.remove("hidden");
  }
  // Commenters and likers the agent fetched, merged into the same per-post store the manual flow uses.
  function absorbEngagement(result) {
    if (!result || !Array.isArray(result.posts)) return;
    result.posts.forEach((p) => { if (p.shortcode) state.engagement.posts[p.shortcode] = p; });
    state.engagement.people = peopleFromPosts();
    state.engagement.meta = result;
    const fetched = Object.keys(state.engagement.posts).length;
    $("engStatus").textContent = `Agent fetched ${plural(result.post_count || 0, "post")} (${plural(fetched, "post")} in total): ` +
      `${plural(result.commenter_count || 0, "commenter")}, ${plural(result.liker_count || 0, "liker")}.${result.message ? " " + result.message : ""}`;
    renderAll();
    updateButtons();
    if (state.result) $("results").classList.remove("hidden");
  }

  // ---- engagement (commenters + likers) -------------------------------------------------
  function startEngagement(codes, { comments, likers }) {
    if (!codes.length || state.source) return;
    const params = new URLSearchParams({ posts: codes.join(","), max_comments: String(comments), likers: likers ? "1" : "0" });
    showProgress("Fetching commenters & likers…", "💬", "");
    const seenCommenters = new Set(), seenLikers = new Set();
    let done = 0;
    listen(`/api/engagement?${params}`, {
      post_start: (d) => {
        const post = state.posts.find((p) => p.shortcode === d.shortcode);
        $("ptitle").textContent = `Post ${d.index + 1} of ${d.total}${post ? ` · @${post.owner}` : ""}`;
      },
      post_done: (r) => {
        state.engagement.posts[r.shortcode] = r;
        done += 1;
        (r.commenters || []).forEach((c) => seenCommenters.add(c.username));
        (r.likers || []).forEach((l) => seenLikers.add(l.username));
        $("pmeta").textContent = `${plural(done, "post")} done · ${plural(seenCommenters.size, "commenter")} · ${plural(seenLikers.size, "liker")} so far`;
      },
      done: (d) => {
        finish();
        state.engagement.people = d.people || [];
        state.engagement.meta = d;
        const fetched = Object.keys(state.engagement.posts).length;
        $("engStatus").textContent = `Fetched ${plural(fetched, "post")}: ${plural(d.commenter_count, "commenter")}, ${plural(d.liker_count, "liker")}.` +
          (d.message ? ` ${d.message}` : "");
        if (d.message) showError(d.message);
        renderAll();
        updateButtons();
      },
    });
  }
  $("engGo").onclick = () => {
    const wanted = Math.max(1, Math.min(100, parseInt($("engPosts").value, 10) || 30));
    const fetched = state.engagement.posts;
    let codes = state.posts.slice(0, wanted).map((p) => p.shortcode).filter(Boolean);
    const fresh = codes.filter((c) => !fetched[c]);
    if (fresh.length && fresh.length < codes.length) codes = fresh;  // only the ones still missing
    startEngagement(codes, { comments: parseInt($("engComments").value, 10) || 0, likers: $("engLikers").checked });
  };
  // People from all fetched posts, recomputed client-side so a single-post fetch from the modal also counts.
  function peopleFromPosts() {
    const people = new Map();
    const row = (u) => {
      let r = people.get(u.username);
      if (!r) { r = { ...u, comment_count: 0, liked_count: 0, posts_commented: [], posts_liked: [] }; delete r.comments; people.set(u.username, r); }
      return r;
    };
    Object.values(state.engagement.posts).forEach((p) => {
      (p.commenters || []).forEach((c) => { const r = row(c); r.comment_count += c.comment_count || 0; r.posts_commented.push(p.shortcode); });
      (p.likers || []).forEach((l) => { if (!l.username) return; const r = row(l); r.liked_count += 1; r.posts_liked.push(p.shortcode); });
    });
    return [...people.values()];
  }

  // ---- combined account list -----------------------------------------------------------
  const score = (a) => a.post_count * 3 + a.comment_count * 2 + a.liked_count;
  function accounts() {
    const map = new Map();
    state.users.forEach((u) => map.set(u.username, { ...u, roles: ["poster"], comment_count: 0, liked_count: 0, posts_commented: [], posts_liked: [], hashtags: u.hashtags || [] }));
    peopleFromPosts().forEach((p) => {
      const row = map.get(p.username) || { ...p, roles: [], post_count: 0, hashtags: [], posts: [] };
      row.comment_count = p.comment_count; row.liked_count = p.liked_count;
      row.posts_commented = p.posts_commented; row.posts_liked = p.posts_liked;
      ["full_name", "profile_pic_url", "is_verified", "is_private", "user_id", "profile_url"].forEach((k) => { if (row[k] == null && p[k] != null) row[k] = p[k]; });
      if (p.comment_count) row.roles.push("commenter");
      if (p.liked_count) row.roles.push("liker");
      map.set(p.username, row);
    });
    return [...map.values()].sort((a, b) => score(b) - score(a) || a.username.localeCompare(b.username));
  }
  function visibleAccounts() {
    const q = ($("userFilter").value || "").trim().toLowerCase();
    return accounts().filter((a) =>
      (state.roleFilter === "all" || a.roles.includes(state.roleFilter)) &&
      (!q || a.username.toLowerCase().includes(q) || (a.full_name || "").toLowerCase().includes(q)));
  }

  function renderAll() {
    const all = accounts();
    $("sPosts").textContent = fmt(state.posts.length);
    $("sUsers").textContent = fmt(state.users.length);
    const hasEng = Object.keys(state.engagement.posts).length > 0;
    document.querySelectorAll(".stat.eng").forEach((el) => el.classList.toggle("hidden", !hasEng));
    $("sCommenters").textContent = fmt(all.filter((a) => a.comment_count).length);
    $("sLikers").textContent = fmt(all.filter((a) => a.liked_count).length);
    renderPosts();
    renderUsers();
  }

  function renderPosts() {
    const grid = $("postsGrid");
    grid.innerHTML = state.posts.length
      ? state.posts.map(tile).join("")
      : '<div class="empty">No posts came back for these hashtags.</div>';
    grid.querySelectorAll(".tile").forEach((el) => { el.onclick = () => openModal(state.posts[+el.dataset.i]); });
    wireImages(grid);
  }
  function tile(p, i) {
    const badge = p.type === "carousel" ? ICON.carousel : (p.type === "video" ? ICON.video : "");
    const body = p.image_url ? img(p.image_url) : `<div class="caption">${esc(p.caption || "No preview")}</div>`;
    const e = state.engagement.posts[p.shortcode];
    const eng = e && !e.error ? `<div class="engbadge" title="commenters · likers fetched">💬 ${(e.commenters || []).length} · ♥ ${(e.likers || []).length}</div>` : "";
    return `<div class="tile ${p.image_url ? "" : "noimg"}" data-i="${i}" title="@${esc(p.owner || "")}">
      ${body}<span class="badge">${badge}</span>${eng}
      <div class="overlay"><span>${ICON.heart} ${fmt(p.likes)}</span><span>${ICON.comment} ${fmt(p.comments)}</span></div>
      <div class="owner">@${esc(p.owner || "unknown")}</div>
    </div>`;
  }

  function renderUsers() {
    const rows = visibleAccounts();
    const list = $("usersList");
    list.innerHTML = rows.length ? rows.map(userRow).join("") : '<div class="user"><div class="usub">No accounts match.</div></div>';
    $("roleCount").textContent = plural(rows.length, "account");
    list.querySelectorAll(".pbtn").forEach((b) => { b.onclick = () => toggleProfile(b.dataset.user); });
    wireImages(list);
  }
  function userRow(u) {
    const prof = state.profiles[u.username];
    const bits = [];
    if (prof && prof.followers != null) bits.push(`${fmt(prof.followers)} followers`);
    if (u.post_count) bits.push(plural(u.post_count, "post"));
    if (u.comment_count) bits.push(plural(u.comment_count, "comment"));
    if (u.liked_count) bits.push(`liked ${plural(u.liked_count, "post")}`);
    const sub = [u.full_name || (prof && prof.full_name), bits.join(", ")].filter(Boolean).join(" · ");
    const roles = u.roles.map((r) => `<span class="role ${r}">${r}</span>`).join("");
    const url = u.profile_url || profileUrl(u.username);
    const open = state.openProfiles.has(u.username);
    const loading = state.loadingProfiles.has(u.username);
    const card = loading ? '<div class="pcard"><span class="spin"></span> Loading the profile…</div>' : (open && prof ? profileCard(prof) : "");
    return `<div class="user">${avatar(u.username, u.profile_pic_url)}
      <div class="uinfo">
        <div class="uname"><a href="${esc(url)}" target="_blank" rel="noopener">${esc(u.username)}</a>${u.is_verified ? ICON.verified : ""}${u.is_private ? ICON.lock : ""}<span class="rolechips">${roles}</span></div>
        <div class="usub">${esc(sub)}</div>
        <div class="utags">${(u.hashtags || []).map((t) => `<span>#${esc(t)}</span>`).join("")}</div>
      </div>
      <button class="btn pbtn" data-user="${esc(u.username)}" ${loading ? "disabled" : ""}>${open && prof ? "Hide" : "Profile"}</button>
      <a class="btn follow" href="${esc(url)}" target="_blank" rel="noopener">View</a>
    </div>${card}`;
  }

  // ---- profiles ------------------------------------------------------------------------
  const safeUrl = (u) => /^https?:\/\//i.test(String(u || ""));
  function profileCard(p) {
    const kind = [p.is_verified ? "Verified" : "", p.is_business_account ? "Business" : (p.is_professional_account ? "Creator" : ""),
      p.is_private ? "Private" : "", p.category, p.city].filter(Boolean).join(" · ");
    const links = ((p.bio_links && p.bio_links.length) ? p.bio_links : (p.external_url ? [{ url: p.external_url }] : []))
      .filter((l) => safeUrl(l.url))
      .map((l) => `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.title || l.url.replace(/^https?:\/\//i, ""))}</a>`).join("");
    return `<div class="pcard">
      <div class="pstats"><span><b>${fmt(p.posts_count)}</b> posts</span><span><b>${fmt(p.followers)}</b> followers</span><span><b>${fmt(p.following)}</b> following</span></div>
      ${p.full_name ? `<div class="pname">${esc(p.full_name)}</div>` : ""}
      ${kind ? `<div class="usub">${esc(kind)}</div>` : ""}
      ${p.biography ? `<div class="pbio" dir="auto">${esc(p.biography)}</div>` : ""}
      ${links ? `<div class="plinks">${links}</div>` : ""}
    </div>`;
  }
  function absorbProfile(p) {
    if (!p || !p.username) return;
    state.profiles[p.username] = p;
    state.openProfiles.add(p.username);
    if (state.result) renderUsers();
  }
  async function toggleProfile(username) {
    if (state.profiles[username]) {
      if (state.openProfiles.has(username)) state.openProfiles.delete(username); else state.openProfiles.add(username);
      renderUsers();
      return;
    }
    if (state.source || state.agent) { showError("Wait for the current crawl or agent run to finish, then load the profile."); return; }
    hideError();
    state.loadingProfiles.add(username);
    renderUsers();
    try {
      const res = await fetch(`/api/profile?username=${encodeURIComponent(username)}`);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      state.profiles[username] = body;
      state.openProfiles.add(username);
    } catch (e) {
      showError(`@${username}: ${e.message || e}`);
    } finally {
      state.loadingProfiles.delete(username);
      renderUsers();
    }
  }
  $("userFilter").oninput = renderUsers;
  document.querySelectorAll("#roles .pill").forEach((b) => {
    b.onclick = () => {
      state.roleFilter = b.dataset.role;
      document.querySelectorAll("#roles .pill").forEach((x) => x.classList.toggle("active", x === b));
      renderUsers();
    };
  });

  function showTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $("postsGrid").classList.toggle("hidden", name !== "posts");
    $("usersPanel").classList.toggle("hidden", name !== "users");
  }
  document.querySelectorAll(".tab").forEach((t) => { t.onclick = () => showTab(t.dataset.tab); });

  // ---- post modal ----------------------------------------------------------------
  let modalPost = null;
  function openModal(p) {
    modalPost = p;
    const media = p.video_url
      ? `<video src="${esc(p.video_url)}" controls playsinline poster="${esc(p.image_url || "")}"></video>`
      : (p.image_url ? img(p.image_url, 'loading="eager"') : '<div class="nomedia">No preview available</div>');
    $("modalMedia").innerHTML = media;
    wireImages($("modalMedia"));
    renderModalSide();
    $("modal").classList.remove("hidden");
  }
  function renderModalSide() {
    const p = modalPost;
    if (!p) return;
    const u = state.users.find((x) => x.username === p.owner);
    const e = state.engagement.posts[p.shortcode];
    $("modalSide").innerHTML = `
      <div class="mhead">${avatar(p.owner, u && u.profile_pic_url)}
        <div class="uinfo">
          <div class="uname"><a href="${esc(profileUrl(p.owner))}" target="_blank" rel="noopener">${esc(p.owner || "unknown")}</a>${u && u.is_verified ? ICON.verified : ""}</div>
          ${p.location ? `<div class="usub">${esc(p.location)}</div>` : ""}
        </div>
      </div>
      <div class="mbody">
        <div>${p.caption ? esc(p.caption) : '<span class="usub">No caption</span>'}</div>
        ${engagementSections(p, e)}
      </div>
      <div class="mstats">
        <span><b>${fmt(p.likes)}</b> likes</span><span><b>${fmt(p.comments)}</b> comments</span>
        ${p.views != null ? `<span><b>${fmt(p.views)}</b> views</span>` : ""}
        <span>${esc(p.type || "")}${p.carousel ? ` · ${plural(p.carousel.length, "item")}` : ""}</span>
        <span>${date(p.taken_at)}</span>
        <span>${(p.hashtags || []).map((t) => "#" + esc(t)).join(" ")}</span>
      </div>
      <div class="mlink"><a href="${esc(p.url)}" target="_blank" rel="noopener">Open on Instagram ↗</a></div>`;
    const btn = $("modalSide").querySelector("#fetchOne");
    if (btn) btn.onclick = () => { closeModal(); startEngagement([p.shortcode], { comments: parseInt($("engComments").value, 10) || 30, likers: $("engLikers").checked }); };
    wireImages($("modalSide"));
  }
  function engagementSections(p, e) {
    if (!e) {
      return `<div class="msection"><h4>Commenters &amp; likers</h4>
        <button id="fetchOne" class="btn" ${state.source || !state.loggedIn ? "disabled" : ""}>Fetch for this post</button></div>`;
    }
    if (e.error) return `<div class="msection"><h4>Commenters &amp; likers</h4><div class="usub">${esc(e.error)}</div></div>`;
    const comments = (e.comments || []).map((c) => `<div class="comment">${avatar(c.username, c.profile_pic_url)}
        <div class="ctext"><a href="${esc(profileUrl(c.username))}" target="_blank" rel="noopener">${esc(c.username || "?")}</a>${c.is_verified ? ICON.verified : ""} ${esc(c.text || "")}
          <div class="cmeta">${date(c.created_at)}${c.likes ? ` · ${plural(c.likes, "like")}` : ""}${c.replies ? ` · ${plural(c.replies, "reply").replace("replys", "replies")}` : ""}</div></div></div>`).join("");
    const likers = (e.likers || []).map((l) => `<a class="likerchip" href="${esc(profileUrl(l.username))}" target="_blank" rel="noopener" title="${esc(l.full_name || "")}">${avatar(l.username, l.profile_pic_url)}<span>${esc(l.username)}</span></a>`).join("");
    const cHead = e.comment_count != null ? `Comments · ${fmt((e.comments || []).length)} of ${fmt(e.comment_count)}` : `Comments · ${(e.comments || []).length}`;
    const lHead = e.like_count != null ? `Liked by · ${fmt((e.likers || []).length)} of ${fmt(e.like_count)}` : `Liked by · ${(e.likers || []).length}`;
    return `<div class="msection"><h4>${cHead}${e.comments_truncated ? " (more on Instagram)" : ""}</h4>${comments || '<div class="usub">No comments fetched.</div>'}</div>
      <div class="msection"><h4>${lHead}</h4><div class="likers">${likers || '<div class="usub">No likers fetched.</div>'}</div>
      ${e.likers_truncated ? '<div class="more">Instagram only exposes the first ~100 likers on the web.</div>' : ""}</div>`;
  }
  function closeModal() { $("modal").classList.add("hidden"); $("modalMedia").innerHTML = ""; modalPost = null; }
  document.querySelectorAll("[data-close]").forEach((el) => { el.onclick = closeModal; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

  // ---- export --------------------------------------------------------------------
  function download(name, content, type) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([content], { type }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function csv(rows) {
    const cols = ["username", "user_id", "full_name", "roles", "post_count", "comment_count", "liked_count", "hashtags", "is_private", "is_verified",
      "followers", "following", "posts_count", "biography", "external_url", "profile_url"];
    const cell = (v) => {
      v = Array.isArray(v) ? v.join(" ") : (v ?? "");
      v = String(v);
      return /[",\n\r]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
    };
    const value = (r, c) => {
      if (c === "profile_url") return r[c] || profileUrl(r.username);
      return r[c] ?? (state.profiles[r.username] || {})[c];  // profile columns are filled once a profile was loaded
    };
    return [cols.join(","), ...rows.map((r) => cols.map((c) => cell(value(r, c))).join(","))].join("\n");
  }
  const stamp = () => new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  $("copyUsers").onclick = async () => {
    try { await navigator.clipboard.writeText(visibleAccounts().map((u) => u.username).join("\n")); flash($("copyUsers"), "Copied!"); }
    catch { showError("Clipboard blocked by the browser; use CSV instead."); }
  };
  $("dlCsv").onclick = () => download(`hashtag-accounts-${stamp()}.csv`, csv(visibleAccounts()), "text/csv");
  $("dlJson").onclick = () => download(`hashtag-scrape-${stamp()}.json`, JSON.stringify({
    ...state.result,
    accounts: accounts(),
    engagement: { posts: Object.values(state.engagement.posts), people: peopleFromPosts() },
    profiles: state.profiles,
  }, null, 2), "application/json");

  // ---- agent chat -------------------------------------------------------------------
  const chat = $("chat"), questionBox = $("question");
  function setAgentInfo(info) {
    state.agentReady = !!(info && info.configured);
    const models = (info && info.models) || [];
    $("agentModel").textContent = state.agentReady
      ? `Model: ${models[0]}${models.length > 1 ? ` (+${models.length - 1} fallback${models.length > 2 ? "s" : ""})` : ""}`
        + (info.langsmith ? ` · traced to LangSmith project "${info.langsmith}"` : "")
      : ((info && info.message) || "No LLM configured: set GEMINI_API_KEY or GROQ_API_KEY in .env and restart the server.");
    updateButtons();
  }
  // Small, safe markdown: the text is escaped first, then paragraphs, lists, tables, rules, bold, italics,
  // code, links and @usernames. Unbalanced ** markers are dropped rather than shown.
  function md(text) {
    const inline = (s) => s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*\*([^*]+)\*\*\*/g, "<b><i>$1</i></b>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?![*\w])/g, "$1<i>$2</i>")
      .replace(/\*{2,}/g, "")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(,;:])@([A-Za-z0-9_.]{2,30})/g, (m, pre, u) => `${pre}<a href="${profileUrl(u)}" target="_blank" rel="noopener">@${u}</a>`);
    const lines = esc(text || "").split("\n");
    let html = "", list = null, table = null;
    const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
    const closeTable = () => {
      if (!table) return;
      const rows = table.filter((r) => !/^\|?\s*:?-{2,}/.test(r));
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      html += "<table>" + rows.map((r, i) => `<tr>${cells(r).map((c) => `<${i ? "td" : "th"}>${inline(c)}</${i ? "td" : "th"}>`).join("")}</tr>`).join("") + "</table>";
      table = null;
    };
    for (const raw of lines) {
      const line = raw.trimEnd();
      let m;
      if (/^\s*\|.*\|\s*$/.test(line)) { closeList(); (table = table || []).push(line.trim()); continue; }
      closeTable();
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { closeList(); html += "<hr>"; }
      else if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
      else if ((m = line.match(/^#{1,6}\s+(.*)/))) { closeList(); html += `<h4>${inline(m[1])}</h4>`; }
      else if (!line.trim()) { closeList(); }
      else { closeList(); html += `<p>${inline(line)}</p>`; }
    }
    closeList(); closeTable();
    return html;
  }
  function chatShow() {
    chat.classList.remove("hidden");
    $("examples").classList.add("hidden");
    $("chatClear").classList.remove("hidden");
  }
  function bubble(cls, html) {
    chatShow();
    const el = document.createElement("div");
    el.className = `msg ${cls}`;
    el.innerHTML = html;
    chat.appendChild(el);
    chat.scrollTop = chat.scrollHeight;
    return el;
  }
  const argsText = (args) => Object.entries(args || {}).map(([k, v]) =>
    `${k}=${Array.isArray(v) ? (v.length > 4 ? `${v.slice(0, 4).join(",")},…(${v.length})` : v.join(",")) : v}`).join(" ");
  const pretty = (s) => { try { return JSON.stringify(typeof s === "string" ? JSON.parse(s) : s, null, 2); } catch { return String(s ?? ""); } };
  // One collapsible trace per question, shaped like a LangSmith trace: the question is the root chain run,
  // each model call an llm run, and the tool calls of one turn a "tools" chain with one tool run each.
  // Every run shows its type, timing and tokens, the model's reasoning, and expandable inputs / outputs.
  const RUN_TYPES = {
    chain: ["Chain", "a sequence of steps"],
    llm: ["LLM", "one call to the model"],
    tool: ["Tool", "one Instagram lookup"],
  };
  const fmtSec = (s) => (s == null ? "" : s < 10 ? `${Number(s).toFixed(2)}s` : `${Number(s).toFixed(1)}s`);
  const fmtNum = (n) => Number(n || 0).toLocaleString("en-US");
  const runBadge = (type) => `<span class="rtype ${esc(type)}">${esc((RUN_TYPES[type] || [type])[0])}</span>`;
  const ioBlock = (label, html) => `<details class="io"><summary>${label}</summary>${html}</details>`;
  const jsonBlock = (v) => `<pre>${esc(pretty(v))}</pre>`;
  function messageList(msgs) {
    return `<div class="mlist">${(msgs || []).map((m) => {
      const text = m.preview || (m.tool_calls ? `→ ${m.tool_calls.join(", ")}` : "");
      const more = m.chars > (m.preview || "").length ? "…" : "";
      return `<div class="mrow"><span class="mrole role-${esc(m.role)}">${esc(m.role)}${m.name ? ` · ${esc(m.name)}` : ""}</span>`
        + `<span class="mprev">${esc(text)}${more}</span><span class="mchars">${fmtNum(m.chars)} chars</span></div>`;
    }).join("")}</div>`;
  }
  const OLD_EVENTS = new Set(["reasoning", "message", "tool_start", "tool_end"]);

  function newTrace(host) {
    if (!host) { chatShow(); host = chat; }
    const el = document.createElement("details");
    el.className = "trace running";
    el.open = true;
    el.innerHTML = `<summary><span class="spin"></span><span class="tlabel">Starting…</span><span class="tcounts"></span></summary>
      <div class="legend">${Object.keys(RUN_TYPES).map((t) => `<span>${runBadge(t)} ${RUN_TYPES[t][1]}</span>`).join("")}</div>
      <div class="tree"></div>`;
    host.appendChild(el);
    host.scrollTop = host.scrollHeight;
    return {
      el, host, tree: el.querySelector(".tree"), runs: new Map(), counts: { chain: 0, llm: 0, tool: 0 }, tokens: null, seconds: null,
      label: (t) => { el.querySelector(".tlabel").textContent = t; },
    };
  }
  function traceCounts(trace) {
    trace.el.querySelector(".tcounts").innerHTML = Object.entries(trace.counts)
      .filter(([, n]) => n).map(([t, n]) => `<span>${runBadge(t)}<b>${n}</b></span>`).join("");
  }
  function runStart(trace, d) {
    const parent = d.parent_id && trace.runs.get(d.parent_id);
    const inputs = d.inputs || {};
    const el = document.createElement("div");
    el.className = `run ${d.run_type} running`;
    el.dataset.offset = d.offset || 0;
    let args = "", body = "";
    if (d.run_type === "llm") {
      const models = inputs.models && inputs.models.length > 1 ? `<div class="rmeta">Fallback chain: ${esc(inputs.models.join(" → "))}</div>` : "";
      const choice = inputs.tool_choice ? ` · tool_choice ${esc(inputs.tool_choice)}` : "";
      body = ioBlock(`Input · ${plural((inputs.messages || []).length, "message")}${choice}`, models + messageList(inputs.messages));
    } else if (d.run_type === "tool") {
      args = argsText(inputs);
      body = ioBlock("Input", jsonBlock(inputs));
    } else if (!d.parent_id) {
      const earlier = inputs.history_turns ? `<div class="rmeta">+ ${plural(inputs.history_turns, "earlier chat turn")}</div>` : "";
      body = ioBlock("Input", `<pre>${esc(inputs.question || "")}</pre>${earlier}`);
    }
    el.innerHTML = `<div class="rhead" title="Click to fold">${runBadge(d.run_type)}<span class="rname">${esc(d.name)}</span>`
      + (args ? `<span class="rargs">${esc(args)}</span>` : "")
      + `<span class="rmeta">${d.step ? `step ${esc(d.step)}` : ""}</span>`
      + `<span class="rright"><span class="rtok"></span><span class="rtime"></span><span class="wf"><i></i></span><span class="spin"></span><span class="rstat"></span></span></div>`
      + `<div class="rbody">${body}</div><div class="rkids"></div>`;
    el.querySelector(".rhead").onclick = () => el.classList.toggle("folded");
    ((parent && parent.querySelector(":scope > .rkids")) || trace.tree).appendChild(el);
    trace.runs.set(d.run_id, el);
    trace.counts[d.run_type] = (trace.counts[d.run_type] || 0) + 1;
    traceCounts(trace);
    trace.label(d.run_type === "llm" ? `LLM call · step ${d.step}…` : d.run_type === "tool" ? `Running ${d.name}…` : "Running…");
    trace.host.scrollTop = trace.host.scrollHeight;
  }
  function runEnd(trace, d) {
    const el = trace.runs.get(d.run_id);
    if (!el) return;
    el.classList.remove("running");
    el.classList.add(d.error ? "err" : "ok");
    el.dataset.seconds = d.seconds || 0;
    const head = (sel) => el.querySelector(`:scope > .rhead ${sel}`);
    const body = el.querySelector(":scope > .rbody");
    const add = (html, first) => body.insertAdjacentHTML(first ? "afterbegin" : "beforeend", html);
    head(".rtime").textContent = fmtSec(d.seconds);
    head(".rstat").textContent = d.error ? "✗" : "✓";
    if (d.run_type === "llm") {
      if (d.model) {
        head(".rname").textContent = d.model;
        head(".rmeta").textContent = [d.step ? `step ${d.step}` : "", `via ${d.name}`].filter(Boolean).join(" · ");
      }
      const u = d.usage;
      if (u && u.total != null) {
        head(".rtok").textContent = `${fmtNum(u.total)} tokens`;
        head(".rtok").title = `input ${fmtNum(u.input)} · output ${fmtNum(u.output)}${u.reasoning ? ` (reasoning ${fmtNum(u.reasoning)})` : ""}`;
        trace.tokens = (trace.tokens || 0) + u.total;
      }
      const calls = d.tool_calls || [];
      let html = "";
      if (d.reasoning) html += `<div class="thought"><div class="tl">💭 Reasoning</div><div class="ttext">${md(d.reasoning)}</div></div>`;
      if (calls.length && d.text) html += `<div class="rnote">${md(d.text)}</div>`;
      if (calls.length) html += `<div class="rcalls">→ asks for ${calls.map((c) => `<code>${esc(c.name)}</code>`).join(" ")}</div>`;
      else if (!d.error) html += `<div class="rcalls">→ writes the answer${d.text ? ` (${fmtNum(d.text.length)} chars)` : ""}</div>`;
      add(html, true);
      if (!d.error) add(ioBlock("Output", jsonBlock({ text: d.text, tool_calls: calls, usage: d.usage })));
    } else if (d.run_type === "tool") {
      add(`<div class="rsum">${esc(d.summary || d.error || "done")}</div>`, true);
      if (d.output) add(ioBlock("Output · what the model read", jsonBlock(d.output)));
    } else if (d.parent_id) {
      const o = d.outputs || {};
      head(".rmeta").textContent = [d.step ? `step ${d.step}` : "", plural(o.calls || 0, "call"), o.failed ? `${o.failed} failed` : ""].filter(Boolean).join(" · ");
    } else {
      trace.seconds = d.seconds;
      if (d.tokens != null) head(".rtok").textContent = `${fmtNum(d.tokens)} tokens`;
      if (d.outputs) add(ioBlock("Output", jsonBlock(d.outputs)));
      waterfall(trace, d.seconds);
    }
    if (d.error && d.run_type !== "tool") add(`<div class="rerr">${esc(d.error)}</div>`, true);
    trace.label("Thinking…");
  }
  // Latency bars, like LangSmith's waterfall: where each run started and how long it took, relative to the whole trace.
  function waterfall(trace, total) {
    if (!total) return;
    trace.el.querySelectorAll(".run").forEach((r) => {
      const bar = r.querySelector(":scope > .rhead .wf i");
      const left = Math.min(98, (Number(r.dataset.offset) / total) * 100);
      const width = Math.max(2, Math.min(100 - left, (Number(r.dataset.seconds || 0) / total) * 100));
      bar.style.left = `${left}%`;
      bar.style.width = `${width}%`;
    });
    trace.el.classList.add("timed");
  }
  function traceDone(trace, status) {
    trace.el.classList.remove("running");
    trace.el.querySelectorAll(".run.running").forEach((r) => {
      r.classList.remove("running");
      r.classList.add("err");
      r.querySelector(":scope > .rhead .rstat").textContent = "–";
    });
    if (!trace.runs.size) { trace.el.remove(); return; }
    const parts = [status || "Trace", fmtSec(trace.seconds), trace.tokens != null ? `${fmtNum(trace.tokens)} tokens` : ""];
    trace.label(parts.filter(Boolean).join(" · "));
  }
  // Read a fetch() body as server-sent events (EventSource cannot POST).
  async function* sseEvents(body) {
    const reader = body.getReader(), decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        let event = "message", data = "";
        raw.split("\n").forEach((l) => { if (l.startsWith("event:")) event = l.slice(6).trim(); else if (l.startsWith("data:")) data += l.slice(5).trim(); });
        if (data) yield { event, data: JSON.parse(data) };
      }
    }
  }
  async function askAgent(question) {
    question = (question || "").trim();
    if (!question || state.agent || !state.agentReady || !state.loggedIn) return;
    hideError();
    bubble("user", esc(question));
    const trace = newTrace();
    const ctrl = new AbortController();
    state.agent = ctrl;
    const history = state.chat.slice(-10);
    state.chat.push({ role: "user", content: question });
    questionBox.value = "";
    $("ask").classList.add("hidden"); $("askStop").classList.remove("hidden");
    updateButtons();
    let answered = false;
    try {
      const res = await fetch("/api/agent", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, history }), signal: ctrl.signal,
      });
      if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.error || `HTTP ${res.status}`); }
      for await (const { event, data } of sseEvents(res.body)) {
        if (event === "run_start") runStart(trace, data);
        else if (event === "run_end") {
          runEnd(trace, data);
          if (data.run_type === "tool" && data.result) {
            if (data.name === "search_hashtags") absorbScrape(data.result);
            else if (data.name === "get_engagement") absorbEngagement(data.result);
            else if (data.name === "get_profile") absorbProfile(data.result);
          }
        }
        else if (OLD_EVENTS.has(event) && !trace.stale) {
          // a server started before the run tree existed: it still streams the old event types
          trace.stale = true;
          bubble("err", "This server is still running older code, so the trace stays empty. Restart it (Ctrl+C, then python -m app.web) and reload the page.");
        }
        else if (event === "final") {
          answered = true;
          traceDone(trace);
          const meta = [data.model, data.tokens != null ? `${fmtNum(data.tokens)} tokens` : "", data.stopped ? `stopped: ${data.stopped}` : ""].filter(Boolean).join(" · ");
          bubble("agent", md(data.text) + `<div class="mmeta">${esc(meta)}</div>`);
          state.chat.push({ role: "assistant", content: data.text || "" });
        }
        else if (event === "failed") { answered = true; traceDone(trace, "Failed"); bubble("err", `${esc(data.kind)}: ${esc(data.message)}`); }
      }
      if (!answered) { traceDone(trace, "Interrupted"); bubble("err", "The server closed the stream before answering. Check its terminal."); }
    } catch (e) {
      traceDone(trace, e.name === "AbortError" ? "Stopped" : "Failed");
      if (e.name === "AbortError") bubble("note", "Stopped.");
      else bubble("err", esc(e.message || String(e)));
    } finally {
      state.agent = null;
      $("ask").classList.remove("hidden"); $("askStop").classList.add("hidden");
      updateButtons();
      questionBox.focus();
    }
  }
  $("askForm").onsubmit = (e) => { e.preventDefault(); askAgent(questionBox.value); };
  questionBox.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askAgent(questionBox.value); } });
  questionBox.addEventListener("input", updateButtons);
  $("askStop").onclick = () => { if (state.agent) state.agent.abort(); };
  $("chatClear").onclick = () => {
    if (state.agent) return;
    state.chat = []; chat.innerHTML = ""; chat.classList.add("hidden");
    $("examples").classList.remove("hidden"); $("chatClear").classList.add("hidden");
  };
  document.querySelectorAll(".example").forEach((b) => { b.onclick = () => { questionBox.value = b.textContent; updateButtons(); askAgent(b.textContent); }; });

  // ---- hashtag map ---------------------------------------------------------------------------
  // The mapping agent explores hashtag by hashtag and keeps the map in the graph database. Its "graph"
  // events carry the map after every change, so the graph and the node table grow while it works.
  const map = { snap: null, view: null, selected: null };
  const mapStatus = (s) => (s === "running" ? "Building…" : s === "done" ? "Done" : s ? s[0].toUpperCase() + s.slice(1) : "");
  const when = (iso) => { const d = new Date(/Z|[+-]\d\d:?\d\d$/.test(iso || "") ? iso : `${iso}Z`); return isNaN(d) ? "" : d.toLocaleString(); };
  function showView(name) {
    document.querySelectorAll(".apptab").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    $("viewScout").classList.toggle("hidden", name !== "scout");
    $("viewMap").classList.toggle("hidden", name !== "map");
    history.replaceState(null, "", name === "map" ? "#map" : location.pathname + location.search);
    if (name === "map") { loadMapList(); $("mapSeeds").focus(); }
  }
  document.querySelectorAll(".apptab").forEach((b) => { b.onclick = () => showView(b.dataset.view); });

  function mapError(msg) { const el = $("mapError"); el.textContent = msg || ""; el.classList.toggle("hidden", !msg); }
  function graphView() {
    if (!map.view) map.view = new window.HashtagGraphView($("mapGraph"), { onSelect: (tag) => selectNode(tag, false) });
    return map.view;
  }
  function renderMap(snap) {
    map.snap = snap;
    $("mapResult").classList.remove("hidden");
    const stat = (value, label) => `<div class="stat"><b>${value}</b><span>${label}</span></div>`;
    const searched = snap.nodes.filter((n) => n.explored).length;
    $("mapStats").innerHTML = stat(`${snap.nodes.length}<small>/${snap.max_nodes}</small>`, "nodes")
      + stat(fmtNum(snap.edges.length), "links")
      + stat(`${snap.searches}${snap.max_searches ? `<small>/${snap.max_searches}</small>` : ""}`, "searches")
      + stat(fmtNum(snap.posts), "posts read")
      + `<div class="mapid"><b>#${esc(snap.seeds.join(" #"))}</b><span class="mstate ${esc((snap.status || "").split(" ")[0])}">${esc(mapStatus(snap.status))}</span>`
      + `${snap.updated_at ? `<span class="usub">saved ${esc(when(snap.updated_at))}</span>` : snap.saved === false ? '<span class="usub">not saved</span>' : ""}</div>`;
    graphView().setData(snap);
    $("mapTableNote").textContent = `${plural(snap.nodes.length, "hashtag")} · ${searched} searched · most frequent first`;
    renderNodeTable(snap);
    nodeDetail(map.selected);
    $("mapJson").disabled = false;
    if (!state.mapRun) showSummary(snap.summary);
  }
  function showSummary(text, meta) {
    $("mapSummary").innerHTML = text ? md(text) + (meta ? `<div class="mmeta">${esc(meta)}</div>` : "") : "";
    $("mapSummary").classList.toggle("hidden", !text);
  }
  function renderNodeTable(snap) {
    const rows = [...snap.nodes].sort((a, b) => b.count - a.count || a.order - b.order);
    const top = Math.max(1, ...rows.map((n) => n.count));
    const role = (n) => (n.seed ? "seed" : n.explored ? "searched" : "found");
    $("mapTable").innerHTML = `<thead><tr><th>#</th><th>Hashtag</th><th>Count</th><th>Searched</th><th>Links</th><th>Found via</th></tr></thead><tbody>`
      + rows.map((n, i) => `<tr data-tag="${esc(n.tag)}" class="${n.tag === map.selected ? "sel" : ""}">`
        + `<td class="rank">${i + 1}</td>`
        + `<td class="htag"><i class="gdot ${role(n)}"></i><a href="https://www.instagram.com/explore/tags/${encodeURIComponent(n.tag)}/" target="_blank" rel="noopener">#${esc(n.tag)}</a>${n.seed ? '<span class="seedtag">seed</span>' : ""}</td>`
        + `<td class="cnt"><span class="cbar"><i style="width:${((n.count / top) * 100).toFixed(1)}%"></i></span><b>${fmtNum(n.count)}</b></td>`
        + `<td>${n.explored ? `✓ ${plural(n.posts_read, "post")}` : '<span class="muted">–</span>'}</td>`
        + `<td>${n.degree}</td>`
        + `<td>${n.found_via ? `#${esc(n.found_via)}` : '<span class="muted">—</span>'}</td></tr>`).join("")
      + "</tbody>";
  }
  $("mapTable").addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-tag]");
    if (row && !e.target.closest("a")) selectNode(row.dataset.tag, true);
  });
  function selectNode(tag, fromList) {
    if (fromList) { graphView().select(tag, false); tag = map.view.selected; }
    map.selected = tag || null;
    $("mapTable").querySelectorAll("tr[data-tag]").forEach((r) => r.classList.toggle("sel", r.dataset.tag === map.selected));
    nodeDetail(map.selected);
  }
  function nodeDetail(tag) {
    const snap = map.snap, n = snap && tag && snap.nodes.find((x) => x.tag === tag);
    if (!n) {
      $("mapDetail").innerHTML = '<span class="muted">Hover a hashtag to light up its links; click one (here or in the table) to list them.</span>';
      return;
    }
    const byCount = (a, b) => b.count - a.count;
    const out = snap.edges.filter((e) => e.source === tag).sort(byCount);
    const inc = snap.edges.filter((e) => e.target === tag).sort(byCount);
    const chips = (list, key) => list.map((e) => `<button type="button" class="lchip" data-tag="${esc(e[key])}">#${esc(e[key])} <b>${e.count}</b></button>`).join("")
      || '<span class="muted">none yet</span>';
    $("mapDetail").innerHTML = `<div class="dhead"><b>#${esc(tag)}</b> · count ${fmtNum(n.count)} · `
      + `${n.explored ? `searched, ${plural(n.posts_read, "post")} read` : "not searched yet"}`
      + `${n.found_via ? ` · found via #${esc(n.found_via)}` : ""} · depth ${n.depth}</div>`
      + (n.explored ? `<div class="drow"><span>Its posts also carry</span>${chips(out, "target")}</div>` : "")
      + `<div class="drow"><span>Carried by posts of</span>${chips(inc, "source")}</div>`;
  }
  $("mapDetail").addEventListener("click", (e) => { const b = e.target.closest(".lchip"); if (b) selectNode(b.dataset.tag, true); });
  $("mapMinLink").addEventListener("input", () => graphView().setMinWeight($("mapMinLink").value));
  $("mapFit").onclick = () => graphView().fit();
  $("mapReset").onclick = () => graphView().reset();

  async function loadMapList(select) {
    try {
      const res = await fetch("/api/maps");
      if (res.status === 404) { $("mapDb").textContent = "This server is running older code without the hashtag map: restart it."; return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      $("mapDb").textContent = `Graph database: ${data.backend} · ${data.path}`;
      const list = $("mapList"), current = select || list.value;
      list.innerHTML = `<option value="">${data.maps.length ? `Saved maps (${data.maps.length})…` : "No saved maps yet"}</option>`
        + data.maps.map((m) => `<option value="${esc(m.id)}">#${esc(m.seeds.join(" #"))} · ${plural(m.nodes, "node")} · ${esc(mapStatus(m.status))} · ${esc(when(m.updated_at))}</option>`).join("");
      if (current && data.maps.some((m) => m.id === current)) list.value = current;
      $("mapDelete").disabled = !list.value;
    } catch (err) {
      $("mapDb").textContent = `Graph database unavailable: ${err.message || err}`;
    }
  }
  $("mapList").onchange = async () => {
    const id = $("mapList").value;
    $("mapDelete").disabled = !id;
    if (!id || state.mapRun) return;
    try {
      const res = await fetch(`/api/maps/${encodeURIComponent(id)}`);
      const snap = await res.json();
      if (!res.ok) throw new Error(snap.error || `HTTP ${res.status}`);
      mapError(null);
      map.selected = null;
      graphView().clear();
      $("mapTrace").innerHTML = ""; $("mapTrace").classList.add("hidden");
      $("mapAgentNote").textContent = `stored ${when(snap.updated_at)}`;
      renderMap(snap);
      $("mapSeeds").value = snap.seeds.join(", ");
    } catch (err) { mapError(err.message || String(err)); }
  };
  $("mapDelete").onclick = async () => {
    const id = $("mapList").value;
    if (!id || state.mapRun || !confirm(`Delete the map #${id} from the graph database?`)) return;
    const res = await fetch(`/api/maps/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) { const err = await res.json().catch(() => ({})); mapError(err.error || `HTTP ${res.status}`); return; }
    if (map.snap && map.snap.id === id) { map.snap = null; graphView().clear(); $("mapResult").classList.add("hidden"); $("mapJson").disabled = true; }
    loadMapList("");
  };
  $("mapJson").onclick = () => {
    if (map.snap) download(`hashtag-map-${map.snap.id}-${stamp()}.json`, JSON.stringify(map.snap, null, 2), "application/json");
  };

  async function buildMap() {
    const seeds = $("mapSeeds").value.trim();
    if (!seeds || state.mapRun || state.agent || state.source || !state.loggedIn || !state.agentReady) return;
    mapError(null);
    const ctrl = new AbortController();
    state.mapRun = ctrl;
    $("mapGo").classList.add("hidden"); $("mapStop").classList.remove("hidden");
    updateButtons();
    map.selected = null;
    if (map.view) map.view.clear();
    showSummary("");
    $("mapResult").classList.remove("hidden");
    $("mapStats").innerHTML = '<span class="spin"></span> Starting the agent…';
    const box = $("mapTrace");
    box.innerHTML = ""; box.classList.remove("hidden");
    $("mapAgentNote").textContent = "";
    const trace = newTrace(box);
    let finished = false;
    try {
      const res = await fetch("/api/map", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: ctrl.signal,
        body: JSON.stringify({
          seeds, max_nodes: Number($("mapNodes").value), max_searches: Number($("mapSearches").value),
          posts_per_search: Number($("mapPosts").value),
        }),
      });
      if (res.status === 404 || res.status === 405) throw new Error("This server is running older code without the hashtag map. Restart it (Ctrl+C, then python -m app.web) and reload the page.");
      if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.error || `HTTP ${res.status}`); }
      for await (const { event, data } of sseEvents(res.body)) {
        if (event === "run_start") runStart(trace, data);
        else if (event === "run_end") runEnd(trace, data);
        else if (event === "graph") {
          renderMap(data);
          if (data.save_error) mapError(`Not saved to the graph database: ${data.save_error}`);
        }
        else if (event === "final") {
          finished = true;
          traceDone(trace);
          const meta = [data.model, data.tokens != null ? `${fmtNum(data.tokens)} tokens` : "", data.stopped ? `stopped: ${data.stopped}` : ""].filter(Boolean).join(" · ");
          showSummary(data.text, meta);
        }
        else if (event === "failed") { finished = true; traceDone(trace, "Failed"); mapError(`${data.kind}: ${data.message}`); }
      }
      if (!finished) { traceDone(trace, "Interrupted"); mapError("The server closed the stream before the agent finished. Check its terminal."); }
    } catch (err) {
      traceDone(trace, err.name === "AbortError" ? "Stopped" : "Failed");
      if (err.name !== "AbortError") mapError(err.message || String(err));
    } finally {
      state.mapRun = null;
      $("mapGo").classList.remove("hidden"); $("mapStop").classList.add("hidden");
      updateButtons();
      loadMapList(map.snap ? map.snap.id : "");
    }
  }
  $("mapForm").onsubmit = (e) => { e.preventDefault(); buildMap(); };
  $("mapStop").onclick = () => { if (state.mapRun) state.mapRun.abort(); };
  $("mapSeeds").addEventListener("input", updateButtons);

  // ---- init ----------------------------------------------------------------------
  const preset = new URLSearchParams(location.search).get("tags");
  if (preset) addTags(preset);
  if (location.hash === "#map") showView("map");
  checkSession().then((ok) => { state.loggedIn = ok; updateButtons(); if (location.hash !== "#map") input.focus(); });
})();
