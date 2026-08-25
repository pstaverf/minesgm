const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
    try {
        if (typeof tg.ready === "function") tg.ready();
        if (typeof tg.expand === "function") tg.expand();
    } catch (_) {}
    try {
        if (typeof tg.setHeaderColor === "function") tg.setHeaderColor('#0a0a0a');
    } catch (_) {}
    try {
        if (typeof tg.setBackgroundColor === "function") tg.setBackgroundColor('#0a0a0a');
    } catch (_) {}
}
const ARENA_BALL_SPINUP_MS = 1150;
const ARENA_BALL_LAUNCH_DELAY_MS = 1000;
const ARENA_BALL_MIN_COORD = 0;
const ARENA_BALL_MAX_COORD = 100;
const ARENA_BALL_SIMULATION_FPS = 60;
const ARENA_BALL_RESTITUTION = 0.93;
const ARENA_BALL_DRAG_PER_SECOND = 0.6;
const ARENA_BALL_STOP_SPEED = 5;
const ARENA_BALL_MAX_MOTION_MS = 28000;
const ARENA_CAMERA_FOLLOW_MS = 3000;
const ARENA_CAMERA_FULL_ZOOM_BEFORE_END_MS = 700;
function extractTelegramUser() {
    try {
        if (tg?.initDataUnsafe?.user?.id) {
            const u = tg.initDataUnsafe.user;
            return {
                id: Number(u.id),
                firstName: u.first_name || "Игрок",
                username: u.username || "",
                avatar: u.photo_url || ""
            };
        }
        const rawInit = tg?.initData || "";
        if (rawInit) {
            const params = new URLSearchParams(rawInit);
            const userStr = params.get("user");
            if (userStr) {
                const u = JSON.parse(userStr);
                if (u && u.id) {
                    return {
                        id: Number(u.id),
                        firstName: u.first_name || "Игрок",
                        username: u.username || "",
                        avatar: u.photo_url || ""
                    };
                }
            }
        }
        if (window.location.hash) {
            const hashStr = window.location.hash.substring(1);
            const hashParams = new URLSearchParams(hashStr);
            const tgData = hashParams.get("tgWebAppData");
            if (tgData) {
                const inner = new URLSearchParams(tgData);
                const userStr = inner.get("user");
                if (userStr) {
                    const u = JSON.parse(userStr);
                    if (u && u.id) {
                        return {
                            id: Number(u.id),
                            firstName: u.first_name || "Игрок",
                            username: u.username || "",
                            avatar: u.photo_url || ""
                        };
                    }
                }
            }
        }
        if (window.location.search) {
            const searchParams = new URLSearchParams(window.location.search);
            const tgData = searchParams.get("tgWebAppData");
            if (tgData) {
                const inner = new URLSearchParams(tgData);
                const userStr = inner.get("user");
                if (userStr) {
                    const u = JSON.parse(userStr);
                    if (u && u.id) {
                        return {
                            id: Number(u.id),
                            firstName: u.first_name || "Игрок",
                            username: u.username || "",
                            avatar: u.photo_url || ""
                        };
                    }
                }
            }
            const uid = searchParams.get("user_id") || searchParams.get("id");
            if (uid && Number(uid) > 0) {
                return {
                    id: Number(uid),
                    firstName: searchParams.get("first_name") || searchParams.get("name") || "Игрок",
                    username: searchParams.get("username") || "",
                    avatar: searchParams.get("photo_url") || searchParams.get("avatar") || ""
                };
            }
        }
        const cached = localStorage.getItem("mines_tg_user");
        if (cached) {
            const u = JSON.parse(cached);
            if (u && u.id && u.id !== 100001) {
                return {
                    id: Number(u.id),
                    firstName: u.firstName || u.first_name || "Игрок",
                    username: u.username || "",
                    avatar: u.avatar || u.photo_url || ""
                };
            }
        }
    } catch (e) {
        console.error("extractTelegramUser error:", e);
    }
    return {
        id: 100001,
        firstName: "Игрок",
        username: "",
        avatar: ""
    };
}
function saveUser(u) {
    if (!u || !u.id || u.id === 100001) return;
    try {
        localStorage.setItem("mines_tg_user", JSON.stringify({
            id: u.id,
            firstName: u.firstName,
            username: u.username,
            avatar: u.avatar
        }));
    } catch (_) {}
}
const initialUser = extractTelegramUser();
saveUser(initialUser);
const state = {
    tab: "arena",
    user: {
        id: initialUser.id,
        firstName: initialUser.firstName,
        username: initialUser.username,
        balance: 0,
        avatar: initialUser.avatar
    },
    activeRound: null,
    arenaAnimatingRoundId: "",
    arenaTimeOffsetMs: 0
};
function arenaNowMs() {
    return Date.now() + (state.arenaTimeOffsetMs || 0);
}
function getAvatarUrl(avatar, name = "Игрок", id = 1) {
    if (avatar && typeof avatar === 'string' && (avatar.startsWith('http') || avatar.startsWith('/')) && !avatar.includes('dicebear')) {
        return avatar;
    }
    const cleanName = encodeURIComponent((name || "Игрок").trim().slice(0, 10));
    return `https://ui-avatars.com/api/?name=${cleanName}&background=2563eb&color=fff&bold=true&size=128&rounded=true`;
}
function formatNumber(num) {
    return Number(num || 0).toLocaleString("ru-RU");
}
function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
function triggerHaptic(style = "medium") {
    try {
        const hf = window.Telegram?.WebApp?.HapticFeedback;
        if (hf) {
            if (style === "light" || style === "medium" || style === "heavy" || style === "rigid" || style === "soft") {
                if (typeof hf.impactOccurred === "function") hf.impactOccurred(style);
            } else if (style === "success" || style === "error" || style === "warning") {
                if (typeof hf.notificationOccurred === "function") hf.notificationOccurred(style);
            }
        } else if (navigator && navigator.vibrate) {
            const duration = style === "heavy" ? 45 : (style === "medium" ? 25 : (style === "light" ? 12 : 20));
            navigator.vibrate(duration);
        }
    } catch (_) {}
}
function showToast(text, type = "ok") {
    const box = document.getElementById("toast-center");
    const chip = document.getElementById("toast-chip");
    if (!box || !chip) {
        alert(text);
        return;
    }
    chip.textContent = text;
    chip.className = `toast-chip ${type}`;
    box.classList.add("show");
    setTimeout(() => {
        box.classList.remove("show");
    }, 2800);
}
async function apiRequest(path, method = "GET", body = null) {
    const headers = { "Content-Type": "application/json" };
    if (tg?.initData) {
        try {
            headers["X-Telegram-Init-Data"] = encodeURIComponent(tg.initData);
        } catch (_) {}
    }
    if (state.user?.id) {
        headers["X-User-Id"] = String(state.user.id);
    }
    if (state.user?.firstName) {
        try {
            headers["X-User-Name"] = encodeURIComponent(state.user.firstName);
        } catch (_) {}
    }
    if (state.user?.username) {
        try {
            headers["X-User-Username"] = encodeURIComponent(state.user.username);
        } catch (_) {}
    }
    const options = { method, headers };
    if (body) {
        options.body = JSON.stringify(body);
    }
    const res = await fetch(path, options);
    const text = await res.text();
    let data;
    try {
        data = JSON.parse(text);
    } catch (_) {
        data = { error: text || "Ошибка ответа сервера" };
    }
    if (!res.ok || data.error) {
        throw new Error(data.error || "Ошибка запроса");
    }
    return data;
}
async function fetchUserProfile() {
    try {
        const data = await apiRequest("/api/me");
        if (data && data.user) {
            state.user.id = data.user.id || state.user.id;
            state.user.balance = Number(data.user.balance) || 0;
            if (data.user.first_name && data.user.first_name !== "Игрок") {
                state.user.firstName = data.user.first_name;
            }
            if (data.user.username) {
                state.user.username = data.user.username;
            }
            const tgPhoto = tg?.initDataUnsafe?.user?.photo_url;
            state.user.avatar = tgPhoto || data.user.avatar || getAvatarUrl(null, state.user.firstName, state.user.id);
            saveUser(state.user);
            renderUserHeader();
        }
    } catch (err) {
        console.error("fetchUserProfile err:", err);
    }
}
function renderUserHeader() {
    const balEl = document.getElementById("user-balance-val") || document.getElementById("header-balance-val") || document.getElementById("balance-val");
    if (balEl) balEl.textContent = `${formatNumber(state.user.balance)} m¢`;
    const pBalEl = document.getElementById("profile-balance");
    if (pBalEl) pBalEl.textContent = `${formatNumber(state.user.balance)} m¢`;
    const pName = document.getElementById("profile-name");
    if (pName) pName.textContent = state.user.firstName;
    const pUser = document.getElementById("profile-username");
    if (pUser) pUser.textContent = state.user.username ? `@${state.user.username}` : `ID: ${state.user.id}`;
    const pAv = document.getElementById("profile-avatar");
    if (pAv) pAv.src = state.user.avatar;
}
function switchView(tabName) {
    state.tab = tabName;
    document.querySelectorAll(".panel, .game-view").forEach(el => el.classList.remove("active"));
    document.querySelectorAll(".tab-btn").forEach(el => el.classList.remove("active"));
    const targetPanel = document.getElementById(`panel-${tabName}`) || document.getElementById(`games-${tabName}-view`) || document.getElementById(`view-${tabName}`);
    const targetTab = document.querySelector(`.tab-btn[data-tab="${tabName}"]`) || document.getElementById(`tab-${tabName}`);
    if (targetPanel) targetPanel.classList.add("active");
    if (targetTab) targetTab.classList.add("active");
    triggerHaptic("light");
}
function getArenaZoomStage() {
    return document.getElementById("arena-field-stage") || document.getElementById("arena-stage");
}
function arenaArrowTransform(angle) {
    return `translate(-50%, -50%) rotate(${Number(angle) || 0}deg) translateX(32px)`;
}
function placeArenaBall(target, angle = 0) {
    const ball = document.getElementById("arena-ball");
    const arrow = document.getElementById("arena-ball-arrow");
    if (!ball || !target) return;
    ball.classList.add("live");
    ball.style.left = `${target.x}%`;
    ball.style.top = `${target.y}%`;
    ball.style.transform = `translate(-50%, -50%) rotate(${Number(angle) || 0}deg)`;
    if (arrow) arrow.classList.remove("live");
}
let arenaBounceTimers = [];
function clearArenaBounceTimers() {
    arenaBounceTimers.forEach(t => clearTimeout(t));
    arenaBounceTimers = [];
}
function hideArenaBall() {
    clearArenaBounceTimers();
    const ball = document.getElementById("arena-ball");
    const arrow = document.getElementById("arena-ball-arrow");
    const stage = getArenaZoomStage();
    if (ball) {
        ball.getAnimations().forEach((anim) => anim.cancel());
        ball.classList.remove("live");
        ball.style.left = "50%";
        ball.style.top = "50%";
        ball.style.opacity = "";
        ball.style.transform = "translate(-50%, -50%)";
    }
    if (arrow) {
        arrow.getAnimations().forEach((anim) => anim.cancel());
        arrow.classList.remove("live");
        arrow.style.opacity = "";
    }
    if (stage) {
        resetArenaFieldZoom();
    }
}
function resetArenaFieldZoom() {
    const stage = getArenaZoomStage();
    if (!stage) return;
    stage.getAnimations().forEach((anim) => anim.cancel());
    stage.style.transform = "scale(1)";
    stage.style.transformOrigin = "50% 50%";
}
function buildArenaBallFrames(round) {
    const start = round?.ballSpawnPosition || round?.targetPosition || { x: 50, y: 50 };
    const angle = Number(round.ballAngle || 0);
    const speed = Math.max(0, Number(round.ballSpeed || 1100));
    const durationMs = Math.max(1000, Number(round.ballDuration || 9) * 1000);
    const min = ARENA_BALL_MIN_COORD;
    const max = ARENA_BALL_MAX_COORD;
    let x = Math.max(min, Math.min(max, Number(start.x ?? 50)));
    let y = Math.max(min, Math.min(max, Number(start.y ?? 50)));
    const rad = (angle * Math.PI) / 180;
    let vx = Math.cos(rad) * speed;
    let vy = Math.sin(rad) * speed;
    const spinDirection = Math.sin(rad) >= 0 ? 1 : -1;
    const spinOffset = ARENA_BALL_SPINUP_MS / durationMs;
    const launchOffset = (ARENA_BALL_SPINUP_MS + ARENA_BALL_LAUNCH_DELAY_MS) / durationMs;
    const spinEase1 = spinOffset * 0.22;
    const spinEase2 = spinOffset * 0.5;
    const spinEase3 = spinOffset * 0.76;
    const spinTravel = 6480 * spinDirection;
    const spinEndRotate = angle + spinTravel;
    const points = [
        { x, y, scale: 0.35, opacity: 0, rotate: angle, offset: 0 },
        { x, y, scale: 1.06, opacity: 1, rotate: angle + spinTravel * 0.56, offset: spinEase1 },
        { x, y, scale: 1.03, opacity: 1, rotate: angle + spinTravel * 0.82, offset: spinEase2 },
        { x, y, scale: 1, opacity: 1, rotate: angle + spinTravel * 0.96, offset: spinEase3 },
        { x, y, scale: 1, opacity: 1, rotate: spinEndRotate, offset: spinOffset },
        { x, y, scale: 1, opacity: 1, rotate: spinEndRotate, offset: launchOffset }
    ];
    const dt = 1 / ARENA_BALL_SIMULATION_FPS;
    const maxSteps = Math.max(1, Math.round((ARENA_BALL_MAX_MOTION_MS / 1000) * ARENA_BALL_SIMULATION_FPS));
    const drag = Math.pow(ARENA_BALL_DRAG_PER_SECOND, dt);
    let elapsedMotionMs = 0;
    for (let step = 1; step <= maxSteps; step += 1) {
        x += vx * dt;
        y += vy * dt;
        elapsedMotionMs += dt * 1000;
        if (x < min) {
            x = min + (min - x);
            vx = Math.abs(vx) * ARENA_BALL_RESTITUTION;
        } else if (x > max) {
            x = max - (x - max);
            vx = -Math.abs(vx) * ARENA_BALL_RESTITUTION;
        }
        if (y < min) {
            y = min + (min - y);
            vy = Math.abs(vy) * ARENA_BALL_RESTITUTION;
        } else if (y > max) {
            y = max - (y - max);
            vy = -Math.abs(vy) * ARENA_BALL_RESTITUTION;
        }
        x = Math.max(min, Math.min(max, x));
        y = Math.max(min, Math.min(max, y));
        vx *= drag;
        vy *= drag;
        const isStopped = Math.hypot(vx, vy) <= ARENA_BALL_STOP_SPEED;
        points.push({
            x,
            y,
            scale: 1,
            opacity: 1,
            rotate: spinEndRotate,
            offset: Math.min(1, (ARENA_BALL_SPINUP_MS + ARENA_BALL_LAUNCH_DELAY_MS + elapsedMotionMs) / durationMs)
        });
        if (isStopped) break;
    }
    const lastPoint = points[points.length - 1];
    if (lastPoint && Number(lastPoint.offset || 0) < 1) {
        points.push({ ...lastPoint, offset: 1 });
    }
    return points.map((point) => ({
        left: `${point.x}%`,
        top: `${point.y}%`,
        transform: `translate(-50%, -50%) scale(${point.scale}) rotate(${point.rotate}deg)`
    }));
}
async function animateArenaBall(round, customStage = null, customBall = null, customArrow = null, isReplay = false) {
    const roundKey = String(round?.id || "");
    const ball = customBall || document.getElementById("arena-ball");
    const arrow = customArrow || document.getElementById("arena-ball-arrow");
    const stage = customStage || getArenaZoomStage();
    if (!roundKey || !ball || !stage || !round?.targetPosition) return;
    if (!isReplay && state.arenaAnimatingRoundId === `${roundKey}:live`) return;
    if (!isReplay) state.arenaAnimatingRoundId = `${roundKey}:live`;
    const durationMs = Math.max(1000, Number(round.ballDuration || 9) * 1000);
    const spawnX = round.ballSpawnPosition?.x ?? 50;
    const spawnY = round.ballSpawnPosition?.y ?? 50;
    const spinOffset   = Math.min(0.8, ARENA_BALL_SPINUP_MS / durationMs);
    const launchOffset = Math.min(0.95, (ARENA_BALL_SPINUP_MS + ARENA_BALL_LAUNCH_DELAY_MS) / durationMs);
    const spinupFrames = [
        { left: `${spawnX}%`, top: `${spawnY}%`, opacity: 0, offset: 0,                    transform: `translate(-50%, -50%) scale(0.35)` },
        { left: `${spawnX}%`, top: `${spawnY}%`, opacity: 1, offset: spinOffset * 0.22,    transform: `translate(-50%, -50%) scale(1.06)` },
        { left: `${spawnX}%`, top: `${spawnY}%`, opacity: 1, offset: spinOffset * 0.5,     transform: `translate(-50%, -50%) scale(1.03)` },
        { left: `${spawnX}%`, top: `${spawnY}%`, opacity: 1, offset: spinOffset,           transform: `translate(-50%, -50%) scale(1)` },
        { left: `${spawnX}%`, top: `${spawnY}%`, opacity: 1, offset: launchOffset,         transform: `translate(-50%, -50%) scale(1)` }
    ];
    let frames;
    const serverTraj = round.ballTrajectory;
    if (serverTraj && serverTraj.keyframes && serverTraj.keyframes.length >= 2) {
        const kf = serverTraj.keyframes;
        const motionRange = 1.0 - launchOffset;
        const motionFrames = kf.map((k) => ({
            left:      `${k.x}%`,
            top:       `${k.y}%`,
            opacity:   1,
            offset:    Math.max(launchOffset, Math.min(1, launchOffset + Number(k.t || 0) * motionRange)),
            transform: `translate(-50%, -50%) scale(1) rotate(${k.angle || 0}deg)`
        }));
        frames = [...spinupFrames, ...motionFrames];
        if (frames[frames.length - 1].offset < 1) {
            frames.push({ ...frames[frames.length - 1], offset: 1 });
        }
    } else {
        const rawFrames = buildArenaBallFrames(round);
        frames = rawFrames.map((frame) => ({
            ...frame,
            offset: Math.max(0, Math.min(1, Number(frame.offset || 0)))
        }));
    }
    if (!frames.length || !ball.animate || !stage.animate) {
        placeArenaBall(round.targetPosition, round.ballAngle || 0);
        return;
    }
    ball.getAnimations().forEach((anim) => anim.cancel());
    if (arrow) arrow.getAnimations().forEach((anim) => anim.cancel());
    stage.getAnimations().forEach((anim) => anim.cancel());
    ball.classList.add("live");
    ball.style.display = "block";
    if (arrow) {
        arrow.classList.add("live");
        arrow.style.display = "block";
    }
    stage.style.transformOrigin = "50% 50%";
    ball.animate(frames, {
        duration: durationMs,
        easing: "linear",
        fill: "both"
    });
    clearArenaBounceTimers();
    if (serverTraj && serverTraj.keyframes) {
        const motionDurationMs = (1.0 - launchOffset) * durationMs;
        const launchDelayMs = launchOffset * durationMs;
        serverTraj.keyframes.forEach(k => {
            if (k.bounced && Number(k.t) > 0) {
                const delay = launchDelayMs + Number(k.t) * motionDurationMs;
                const timer = setTimeout(() => {
                    triggerHaptic("medium");
                }, Math.max(0, delay));
                arenaBounceTimers.push(timer);
            }
        });
    }
    if (arrow && arrow.animate) {
        const angle = Number(round.ballAngle ?? (serverTraj?.keyframes?.[0]?.angle ?? 0));
        const spinDirection = Math.sin((angle * Math.PI) / 180) >= 0 ? 1 : -1;
        const spinTravel = 6480 * spinDirection;
        const spinEndRotate = angle + spinTravel;
        const sp = { left: `${spawnX}%`, top: `${spawnY}%` };
        arrow.animate(
            [
                { ...sp, opacity: 0, transform: arenaArrowTransform(angle),                        offset: 0 },
                { ...sp, opacity: 1, transform: arenaArrowTransform(angle + spinTravel * 0.56),    offset: spinOffset * 0.22 },
                { ...sp, opacity: 1, transform: arenaArrowTransform(angle + spinTravel * 0.82),    offset: spinOffset * 0.5 },
                { ...sp, opacity: 1, transform: arenaArrowTransform(angle + spinTravel * 0.96),    offset: spinOffset * 0.76 },
                { ...sp, opacity: 1, transform: arenaArrowTransform(spinEndRotate),                offset: spinOffset },
                { ...sp, opacity: 1, transform: arenaArrowTransform(spinEndRotate),                offset: Math.max(spinOffset, launchOffset - 0.025) },
                { ...sp, opacity: 0, transform: arenaArrowTransform(spinEndRotate),                offset: launchOffset },
                { ...sp, opacity: 0, transform: arenaArrowTransform(spinEndRotate),                offset: 1 }
            ],
            { duration: durationMs, easing: "linear", fill: "both" }
        );
    }
    const followStartOffset = Math.max(0, 1 - ARENA_CAMERA_FOLLOW_MS / durationMs);
    const fullZoomOffset = Math.max(followStartOffset, 1 - ARENA_CAMERA_FULL_ZOOM_BEFORE_END_MS / durationMs);
    const fullZoomScale = 2.4;
    const cameraFrames = frames.map((frame) => {
        const offset = Number(frame.offset || 0);
        let zoomProgress = 0;
        if (offset >= followStartOffset) {
            zoomProgress = Math.min(1, (offset - followStartOffset) / (fullZoomOffset - followStartOffset || 1));
        }
        const smoothP = zoomProgress * zoomProgress * (3 - 2 * zoomProgress);
        const scale = 1 + (fullZoomScale - 1) * smoothP;
        const x = parseFloat(frame.left) || 50;
        const y = parseFloat(frame.top) || 50;
        const tx = (50 - x) * smoothP * 0.65;
        const ty = (50 - y) * smoothP * 0.65;
        return {
            transform: `scale(${scale}) translate(${tx}%, ${ty}%)`,
            offset
        };
    });
    stage.animate(cameraFrames, {
        duration: durationMs,
        easing: "linear",
        fill: "both"
    });
}
function renderArenaRound(round) {
    const prevStatus = state.activeRound?.status;
    const prevId = state.activeRound?.id;
    state.activeRound = round;
    const roundIdEl = document.getElementById("arena-round-id");
    if (roundIdEl) roundIdEl.textContent = round.id;
    const bankEl = document.getElementById("arena-total-bank");
    if (bankEl) bankEl.textContent = formatNumber(round.totalBank);
    const onlineEl = document.getElementById("arena-online-count");
    if (onlineEl) onlineEl.textContent = `${round.onlineCount || 1} онлайн`;
    const statusPill = document.getElementById("arena-status-pill");
    if (statusPill) {
        statusPill.className = `arena-status-pill ${round.status}`;
        if (round.status === "waiting") statusPill.textContent = "Ожидание";
        else if (round.status === "launching") statusPill.textContent = "Запуск!";
        else if (round.status === "live") statusPill.textContent = "Раунд идёт";
        else if (round.status === "finished") statusPill.textContent = "Победа!";
    }
    const phaseLabel = document.getElementById("arena-phase-label");
    const progressFill = document.getElementById("arena-phase-progress");
    if (round.status === "waiting") {
        state.lastCountdownTick = null;
        if (phaseLabel) phaseLabel.textContent = "Ожидание ставок игроков...";
        if (progressFill) progressFill.style.width = "0%";
    } else if (round.status === "launching") {
        if (round.timeLeft != null && round.timeLeft !== state.lastCountdownTick && round.timeLeft > 0) {
            state.lastCountdownTick = round.timeLeft;
            triggerHaptic("light");
        }
        if (phaseLabel) phaseLabel.textContent = `Запуск шара через ${round.timeLeft} сек...`;
        if (progressFill) {
            const pct = Math.max(0, Math.min(100, (1 - (round.timeLeft / (round.countdownDuration || 15))) * 100));
            progressFill.style.width = `${pct}%`;
        }
    } else if (round.status === "live") {
        state.lastCountdownTick = null;
        if (phaseLabel) phaseLabel.textContent = "Шар катится по арене...";
        if (progressFill) progressFill.style.width = "100%";
    } else if (round.status === "finished") {
        state.lastCountdownTick = null;
        const winnerName = round.winner?.name || "Победитель";
        if (phaseLabel) phaseLabel.textContent = `🎉 Победил: ${winnerName}! Выигрыш: ${formatNumber(round.totalBank)} m¢`;
        if (progressFill) progressFill.style.width = "100%";
    }
    renderZones(round.zones, "arena-zones");
    renderPlayersList(round.players);
    const msgEl = document.getElementById("arena-field-message");
    if (msgEl) {
        // visibility handled via .has-zones CSS class on parent .arena-field
        msgEl.textContent = round.zones && round.zones.length > 0 ? "" : "Сделайте ставку первым!";
    }
    if (round.status === "live" && (prevStatus !== "live" || prevId !== round.id)) {
        animateArenaBall(round);
    } else if (round.status === "waiting" || round.status === "launching") {
        hideArenaBall();
        state.arenaAnimatingRoundId = "";
    }
    if (round.status === "finished" && prevStatus === "live") {
        triggerHaptic("success");
        launchConfetti();
        fetchUserProfile();
    }
}
function renderZones(zones, containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    // Toggle has-zones on the parent arena-field
    const field = container.closest(".arena-field");
    if (!zones || zones.length === 0) {
        container.innerHTML = "";
        if (field) field.classList.remove("has-zones");
        return;
    }
    if (field) field.classList.add("has-zones");
    let html = "";
    zones.forEach(z => {
        const avatar = getAvatarUrl(z.avatar, z.name, z.playerId);
        const lx = z.labelX != null ? z.labelX : z.width / 2;
        const ly = z.labelY != null ? z.labelY : z.height / 2;
        // labelX/labelY are absolute % within field; convert to relative within zone
        const innerLeft = ((lx - z.x) / z.width * 100).toFixed(2);
        const innerTop  = ((ly - z.y) / z.height * 100).toFixed(2);
        html += `
            <div class="arena-zone" style="left:${z.x}%;top:${z.y}%;width:${z.width}%;height:${z.height}%;background-color:${z.color};">
                <div class="arena-zone-shape"></div>
                <div class="arena-zone-inner" style="left:${innerLeft}%;top:${innerTop}%;">
                    <img class="arena-zone-avatar" src="${avatar}" alt="">
                    <div class="arena-zone-label">${escapeHtml(z.name)}</div>
                    <div class="arena-zone-share">${z.share}%</div>
                </div>
            </div>
        `;
    });
    container.innerHTML = html;
}
function renderPlayersList(players) {
    const list = document.getElementById("arena-players-list") || document.getElementById("arena-players");
    const countEl = document.getElementById("arena-players-count");
    if (!list) return;
    if (!players || players.length === 0) {
        list.innerHTML = `<div class="empty-placeholder">Пока нет ставок в текущем раунде</div>`;
        if (countEl) countEl.textContent = "0";
        return;
    }
    if (countEl) countEl.textContent = String(players.length);
    let html = "";
    players.forEach(p => {
        const avatar = getAvatarUrl(p.avatar, p.name, p.id);
        html += `
            <div class="arena-player-card">
                <div class="arena-player-left">
                    <img class="arena-player-avatar" style="border-color:${p.color || '#3b82f6'}" src="${avatar}" alt="">
                    <div>
                        <div class="arena-player-name">${escapeHtml(p.name)}</div>
                        <div class="arena-player-sub">${p.username ? '@' + escapeHtml(p.username) : 'ID: ' + p.id}</div>
                    </div>
                </div>
                <div class="arena-player-right">
                    <div class="arena-player-bet-val">${formatNumber(p.bet)} m¢</div>
                    <div class="arena-player-share-val">${p.share}% шанс</div>
                </div>
            </div>
        `;
    });
    list.innerHTML = html;
}
async function placeArenaBet() {
    triggerHaptic("heavy");
    const input = document.getElementById("arena-mcoin-input");
    const val = parseInt(input?.value || "0", 10);
    if (!val || val <= 0) {
        showToast("Введите сумму ставки!", "error");
        return;
    }
    if (val < 1000) {
        showToast("Минимальная ставка: 1 000 m¢", "error");
        return;
    }
    if (val > state.user.balance) {
        showToast(`Недостаточно mCoin! Баланс: ${formatNumber(state.user.balance)} m¢`, "error");
        return;
    }
    const roundId = state.activeRound?.id || 1;
    const btn = document.getElementById("arena-join-btn");
    if (btn) btn.disabled = true;
    try {
        const data = await apiRequest(`/rounds/${roundId}/join`, "POST", {
            amount: val,
            user_id: state.user.id,
            first_name: state.user.firstName,
            username: state.user.username,
            avatar: state.user.avatar
        });
        if (input) input.value = "";
        showToast("Ставка успешно принята!", "ok");
        triggerHaptic("success");
        if (data.round) renderArenaRound(data.round);
        fetchUserProfile();
    } catch (err) {
        showToast(err.message || "Ошибка ставки", "error");
        triggerHaptic("error");
    } finally {
        if (btn) btn.disabled = false;
    }
}
function setQuickBet(amt) {
    const input = document.getElementById("arena-mcoin-input");
    if (!input) return;
    const cur = parseInt(input.value || "0", 10);
    input.value = cur + amt;
    triggerHaptic("light");
}
function multiplyBet(mult) {
    const input = document.getElementById("arena-mcoin-input");
    if (!input) return;
    const cur = parseInt(input.value || "0", 10) || 1000;
    input.value = Math.max(1000, Math.floor(cur * mult));
    triggerHaptic("light");
}
function setMaxBet() {
    const input = document.getElementById("arena-mcoin-input");
    if (!input) return;
    input.value = state.user.balance;
    triggerHaptic("light");
}
function openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add("open");
    triggerHaptic("light");
}
function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove("open");
    triggerHaptic("light");
}
function closeModalOnOverlay(event, id) {
    if (event.target === event.currentTarget) {
        closeModal(id);
    }
}
function openInfoModal() {
    openModal("modal-info");
}
async function openHistoryModal() {
    openModal("modal-history");
    const container = document.getElementById("arena-history-list");
    if (!container) return;
    container.innerHTML = `<div class="empty-placeholder">Загрузка истории...</div>`;
    try {
        const data = await apiRequest("/api/arena/history");
        if (!data || !data.history || data.history.length === 0) {
            container.innerHTML = `<div class="empty-placeholder">История раундов пуста</div>`;
            return;
        }
        let html = "";
        data.history.forEach(item => {
            const w = item.winner || {};
            const avatar = getAvatarUrl(w.avatar, w.name, w.id);
            html += `
                <div class="history-card">
                    <div class="history-winner-info">
                        <img class="history-winner-avatar" style="border-color:${w.color || '#22c55e'}" src="${avatar}" alt="">
                        <div>
                            <div class="history-title">🏆 ${escapeHtml(w.name || 'Победитель')}</div>
                            <div class="history-meta">#${item.roundId} · ${escapeHtml(item.createdAt || '')} · ${w.share || 0}%</div>
                        </div>
                    </div>
                    <div class="history-right">
                        <div class="history-prize">+${formatNumber(item.totalBank)} m¢</div>
                        <button type="button" class="replay-btn" onclick="openReplayModal(${item.roundId})">Повтор</button>
                    </div>
                </div>
            `;
        });
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<div class="empty-placeholder">Ошибка загрузки истории</div>`;
    }
}
async function openReplayModal(roundId) {
    openModal("modal-replay");
    const roundTitle = document.getElementById("replay-round-id");
    const banner = document.getElementById("replay-winner-banner");
    if (roundTitle) roundTitle.textContent = roundId;
    if (banner) banner.textContent = "Загрузка данных раунда...";
    try {
        const data = await apiRequest(`/api/arena/replay/${roundId}`);
        if (!data || !data.round) return;
        state.replayRound = data.round;
        const w = data.round.winner || {};
        if (banner) {
            banner.textContent = `🏆 Победитель: ${w.name || 'Игрок'} (${w.share || 0}% шанс) · Банк: ${formatNumber(data.round.totalBank)} m¢`;
        }
        renderZones(data.round.zones, "replay-zones");
        const stage = document.getElementById("replay-stage");
        const ball = document.getElementById("replay-ball");
        const arrow = document.getElementById("replay-ball-arrow");
        animateArenaBall(data.round, stage, ball, arrow, true);
    } catch (err) {
        if (banner) banner.textContent = "Ошибка загрузки повтора";
    }
}
function restartReplayAnimation() {
    if (!state.replayRound) return;
    const stage = document.getElementById("replay-stage");
    const ball = document.getElementById("replay-ball");
    const arrow = document.getElementById("replay-ball-arrow");
    animateArenaBall(state.replayRound, stage, ball, arrow, true);
}
function launchConfetti() {
    const canvas = document.getElementById("confetti-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    const colors = ["#1c8cff", "#22c55e", "#f59e0b", "#ec4899", "#a855f7", "#ffffff"];
    const particles = [];
    for (let i = 0; i < 70; i++) {
        particles.push({
            x: canvas.width / 2,
            y: canvas.height * 0.4,
            vx: (Math.random() - 0.5) * 14,
            vy: (Math.random() - 0.7) * 16,
            size: Math.random() * 8 + 4,
            color: colors[Math.floor(Math.random() * colors.length)],
            rotation: Math.random() * 360,
            rotSpeed: (Math.random() - 0.5) * 10,
            opacity: 1
        });
    }
    let start = performance.now();
    function render(now) {
        const elapsed = now - start;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        particles.forEach(p => {
            p.x += p.vx;
            p.y += p.vy;
            p.vy += 0.35; // gravity
            p.rotation += p.rotSpeed;
            p.opacity = Math.max(0, 1 - elapsed / 2200);
            ctx.save();
            ctx.translate(p.x, p.y);
            ctx.rotate((p.rotation * Math.PI) / 180);
            ctx.globalAlpha = p.opacity;
            ctx.fillStyle = p.color;
            ctx.fillRect(-p.size / 2, -p.size / 2, p.size, p.size);
            ctx.restore();
        });
        if (elapsed < 2200) {
            requestAnimationFrame(render);
        } else {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
    }
    requestAnimationFrame(render);
}
let arenaWs = null;
let lastPingTime = 0;
function updatePingUI(ms) {
    const valEl = document.getElementById("arena-ping-val");
    const dotEl = document.getElementById("arena-ping-dot");
    if (valEl) valEl.textContent = String(ms);
    if (dotEl) {
        dotEl.classList.remove("ping-good", "ping-medium", "ping-bad");
        if (ms < 100) {
            dotEl.classList.add("ping-good");
        } else if (ms <= 250) {
            dotEl.classList.add("ping-medium");
        } else {
            dotEl.classList.add("ping-bad");
        }
    }
}
function measurePing() {
    if (arenaWs && arenaWs.readyState === WebSocket.OPEN) {
        lastPingTime = performance.now();
        try {
            arenaWs.send("ping");
        } catch (_) {}
    } else {
        const t0 = performance.now();
        fetch("/rounds/active", { method: "HEAD", cache: "no-store" })
            .then(() => {
                const rtt = Math.max(1, Math.round(performance.now() - t0));
                updatePingUI(rtt);
            })
            .catch(() => {
                updatePingUI(999);
            });
    }
}
function initWebSocket() {
    try {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/ws/arena`;
        arenaWs = new WebSocket(wsUrl);
        arenaWs.onopen = () => {
            measurePing();
        };
        arenaWs.onmessage = (event) => {
            const raw = typeof event.data === "string" ? event.data.trim() : "";
            if (!raw) return;
            if (raw.toLowerCase().startsWith("pong") || raw.toLowerCase().startsWith("ping")) {
                if (lastPingTime > 0) {
                    const rtt = Math.max(1, Math.round(performance.now() - lastPingTime));
                    updatePingUI(rtt);
                }
                return;
            }
            try {
                const data = JSON.parse(raw);
                if (data && data.round) {
                    renderArenaRound(data.round);
                }
            } catch (_) {}
        };
        arenaWs.onclose = () => {
            setTimeout(initWebSocket, 2000);
        };
        arenaWs.onerror = () => {
            try { arenaWs.close(); } catch (_) {}
        };
    } catch (e) {
        console.error("WS error:", e);
    }
}
async function pollActiveRound() {
    try {
        const data = await apiRequest("/rounds/active");
        if (data && data.round) {
            renderArenaRound(data.round);
        }
    } catch (err) {
        console.error("pollActiveRound err:", err);
    }
}
window.switchView = switchView;
window.placeArenaBet = placeArenaBet;
window.setQuickBet = setQuickBet;
window.multiplyBet = multiplyBet;
window.setMaxBet = setMaxBet;
window.openHistoryModal = openHistoryModal;
window.openInfoModal = openInfoModal;
window.closeModal = closeModal;
window.closeModalOnOverlay = closeModalOnOverlay;
window.openReplayModal = openReplayModal;
window.restartReplayAnimation = restartReplayAnimation;
function initApp() {
    const freshUser = extractTelegramUser();
    if (freshUser && freshUser.id !== 100001) {
        state.user.id = freshUser.id;
        state.user.firstName = freshUser.firstName;
        state.user.username = freshUser.username;
        state.user.avatar = freshUser.avatar;
    }
    fetchUserProfile();
    pollActiveRound();
    initWebSocket();
    measurePing();
    setInterval(pollActiveRound, 2000);
    setInterval(fetchUserProfile, 4000);
    setInterval(measurePing, 1000);
}
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initApp);
} else {
    initApp();
}