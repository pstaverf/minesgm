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
        mp_balance: 0,
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
        let msg = "Ошибка запроса";
        if (res.status === 500) {
            msg = "Ошибка сервера, попробуйте снова";
        } else if (text && text.length < 100 && !text.includes("<html")) {
            msg = text;
        }
        data = { error: msg };
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
            if (typeof data.user.mp_balance !== "undefined") {
                state.user.mp_balance = Number(data.user.mp_balance) || 0;
            }
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
    const pMpBal = document.getElementById("profile-mp-balance");
    if (pMpBal) pMpBal.textContent = `${formatNumber(state.user.mp_balance || 0)} MP`;
    const tMpVal = document.getElementById("tasks-mp-val");
    if (tMpVal) tMpVal.textContent = formatNumber(state.user.mp_balance || 0);
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
    if (tabName === "tasks") {
        loadTasks();
    }
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
    if (round.roundHash) {
        state.currentRoundHash = round.roundHash;
        const curHashEl = document.getElementById("fair-cur-hash");
        if (curHashEl && (curHashEl.value === "Генерация..." || curHashEl.value !== round.roundHash)) {
            curHashEl.value = round.roundHash;
        }
        const curRoundEl = document.getElementById("fair-cur-round-id");
        if (curRoundEl) curRoundEl.textContent = round.id;
    }
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
function jsSha256(ascii) {
    function rightRotate(value, amount) {
        return (value >>> amount) | (value << (32 - amount));
    }
    const mathPow = Math.pow;
    const maxWord = mathPow(2, 32);
    let lengthProperty = 'length';
    let i, j;
    let result = '';
    const words = [];
    const asciiBitLength = ascii[lengthProperty] * 8;
    let hash = jsSha256.h = jsSha256.h || [];
    const k = jsSha256.k = jsSha256.k || [];
    let primeCounter = k[lengthProperty];
    const isComposite = {};
    for (let candidate = 2; primeCounter < 64; candidate++) {
        if (!isComposite[candidate]) {
            for (i = 0; i < 313; i += candidate) {
                isComposite[i] = candidate;
            }
            hash[primeCounter] = (mathPow(candidate, .5) * maxWord) | 0;
            k[primeCounter++] = (mathPow(candidate, 1/3) * maxWord) | 0;
        }
    }
    ascii += '\x80';
    while (ascii[lengthProperty] % 64 - 56) ascii += '\x00';
    for (i = 0; i < ascii[lengthProperty]; i++) {
        j = ascii.charCodeAt(i);
        if (j >> 8) return '';
        words[i >> 2] |= j << ((3 - i) % 4) * 8;
    }
    words[words[lengthProperty]] = ((asciiBitLength / maxWord) | 0);
    words[words[lengthProperty]] = (asciiBitLength);
    for (j = 0; j < words[lengthProperty];) {
        const w = words.slice(j, j += 16);
        const oldHash = hash;
        hash = hash.slice(0, 8);
        for (i = 0; i < 64; i++) {
            const w15 = w[i - 15], w2 = w[i - 2];
            const s0 = rightRotate(w15, 7) ^ rightRotate(w15, 18) ^ (w15 >>> 3);
            const s1 = rightRotate(w2, 17) ^ rightRotate(w2, 19) ^ (w2 >>> 10);
            w[i] = i < 16 ? w[i] : (w[i - 16] + s0 + w[i - 7] + s1) | 0;
            const s1_maj = rightRotate(hash[0], 2) ^ rightRotate(hash[0], 13) ^ rightRotate(hash[0], 22);
            const maj = (hash[0] & hash[1]) ^ (hash[0] & hash[2]) ^ (hash[1] & hash[2]);
            const t2 = (s1_maj + maj) | 0;
            const s1_ch = rightRotate(hash[4], 6) ^ rightRotate(hash[4], 11) ^ rightRotate(hash[4], 25);
            const ch = (hash[4] & hash[5]) ^ ((~hash[4]) & hash[6]);
            const t1 = (hash[7] + s1_ch + ch + k[i] + w[i]) | 0;
            hash = [(t1 + t2) | 0].concat(hash);
            hash[4] = (hash[4] + t1) | 0;
        }
        for (i = 0; i < 8; i++) {
            hash[i] = (hash[i] + oldHash[i]) | 0;
        }
    }
    for (i = 0; i < 8; i++) {
        for (let b = 3; b >= 0; b--) {
            const byte = (hash[i] >> (b * 8)) & 255;
            result += (byte < 16 ? '0' : '') + byte.toString(16);
        }
    }
    return result;
}

async function computeSha256(message) {
    try {
        if (window.crypto && window.crypto.subtle) {
            const msgUint8 = new TextEncoder().encode(message);
            const hashBuffer = await window.crypto.subtle.digest('SHA-256', msgUint8);
            const hashArray = Array.from(new Uint8Array(hashBuffer));
            return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
        }
    } catch (_) {}
    return jsSha256(message);
}

function openFairModal() {
    openModal("modal-fair");
    const roundId = state.activeRound?.id || 1;
    const roundHash = state.activeRound?.roundHash || state.currentRoundHash || "";
    const curRoundEl = document.getElementById("fair-cur-round-id");
    const curHashEl = document.getElementById("fair-cur-hash");
    if (curRoundEl) curRoundEl.textContent = roundId;
    if (curHashEl) curHashEl.value = roundHash || "Генерация...";
    const calcRound = document.getElementById("fair-calc-round");
    if (calcRound && !calcRound.value) calcRound.value = roundId;
    renderFairHistory();
}

async function copyFairHash() {
    const curHashEl = document.getElementById("fair-cur-hash");
    const btnText = document.getElementById("fair-copy-btn-text");
    const text = curHashEl?.value || "";
    if (!text || text === "Генерация...") return;
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            curHashEl.select();
            document.execCommand("copy");
        }
        triggerHaptic("success");
        if (btnText) btnText.textContent = "Скопировано!";
        setTimeout(() => {
            if (btnText) btnText.textContent = "Копировать";
        }, 2000);
        showToast("SHA-256 скопирован!", "ok");
    } catch (_) {
        showToast("Не удалось скопировать", "error");
    }
}

function fillFairCalc(roundId, seed) {
    const rEl = document.getElementById("fair-calc-round");
    const sEl = document.getElementById("fair-calc-seed");
    if (rEl) rEl.value = roundId;
    if (sEl) sEl.value = seed;
    triggerHaptic("light");
    calculateSha256InModal();
}

async function calculateSha256InModal() {
    triggerHaptic("light");
    const rEl = document.getElementById("fair-calc-round");
    const sEl = document.getElementById("fair-calc-seed");
    const resBox = document.getElementById("fair-calc-result");
    const hashVal = document.getElementById("fair-calc-hash-val");
    const statusBadge = document.getElementById("fair-calc-status-badge");
    if (!rEl || !sEl || !resBox || !hashVal || !statusBadge) return;
    
    const roundId = rEl.value.trim();
    const seed = sEl.value.trim();
    if (!roundId || !seed) {
        showToast("Заполните номер раунда и сид!", "error");
        return;
    }
    
    const plainString = `${roundId}:${seed}`;
    const calculatedHash = await computeSha256(plainString);
    resBox.style.display = "block";
    hashVal.textContent = calculatedHash;

    let match = false;
    if (state.activeRound && String(state.activeRound.id) === roundId && state.activeRound.roundHash === calculatedHash) {
        match = true;
    } else if (state.fairHistory) {
        const found = state.fairHistory.find(h => String(h.roundId) === roundId);
        if (found && (found.roundHash === calculatedHash || found.serverSeed === seed)) {
            match = true;
        }
    }
    
    if (match) {
        statusBadge.innerHTML = `<span style="color: #22c55e;">✅ Подлинность подтверждена! Хеш совпадает с результатом раунда #${escapeHtml(roundId)}</span>`;
    } else {
        statusBadge.innerHTML = `<span style="color: #60a5fa;">ℹ️ Хеш успешно вычислен: SHA-256("${escapeHtml(plainString)}")</span>`;
    }
}

async function renderFairHistory() {
    const listEl = document.getElementById("fair-history-list");
    if (!listEl) return;
    try {
        const data = await apiRequest("/api/arena/history");
        if (!data || !data.history || data.history.length === 0) {
            listEl.innerHTML = `<div class="empty-placeholder">История раундов пуста</div>`;
            return;
        }
        state.fairHistory = data.history;
        let html = "";
        data.history.forEach(item => {
            const hasSeed = Boolean(item.serverSeed);
            const seedVal = item.serverSeed || "Не раскрыт";
            const hashVal = item.roundHash || "Не сформирован";
            html += `
                <div class="fair-history-item">
                    <div class="fair-history-head">
                        <span class="fair-history-round">Раунд #${item.roundId}</span>
                        <span class="fair-history-badge">✓ SHA-256 MATCH</span>
                    </div>
                    <div class="fair-history-row">
                        <strong>Seed:</strong> <span>${escapeHtml(seedVal)}</span>
                    </div>
                    <div class="fair-history-row">
                        <strong>Hash:</strong> <span>${escapeHtml(hashVal)}</span>
                    </div>
                    ${hasSeed ? `
                    <div style="margin-top: 6px; display: flex; gap: 6px;">
                        <button type="button" class="replay-btn" style="border-color: rgba(34,197,94,0.3); background: rgba(34,197,94,0.12); color: #86efac;" onclick="fillFairCalc(${item.roundId}, '${escapeHtml(item.serverSeed)}')">⚡ Проверить в калькуляторе</button>
                    </div>` : ''}
                </div>
            `;
        });
        listEl.innerHTML = html;
    } catch (_) {
        listEl.innerHTML = `<div class="empty-placeholder">Не удалось загрузить историю раундов</div>`;
    }
}

let activeTasksStatus = {};
let cachedTasksList = [];

async function loadTasks() {
    try {
        const data = await apiRequest("/api/tasks");
        if (data) {
            if (typeof data.mp_balance !== "undefined") {
                state.user.mp_balance = Number(data.mp_balance) || 0;
                renderUserHeader();
            }
            if (Array.isArray(data.tasks)) {
                cachedTasksList = data.tasks;
                renderTasksList(data.tasks);
            }
        }
    } catch (err) {
        console.error("loadTasks error:", err);
    }
}

function renderTasksList(tasks) {
    const listEl = document.getElementById("tasks-sponsor-list");
    if (!listEl) return;
    
    const visibleTasks = (tasks || []).filter(task => !task.is_completed && activeTasksStatus[task.id] !== "deleted");
    
    if (visibleTasks.length === 0) {
        listEl.innerHTML = `<div class="tasks-empty-banner">Нет доступных заданий</div>`;
        return;
    }
    
    let html = "";
    visibleTasks.forEach(task => {
        const status = activeTasksStatus[task.id] || 0;
        
        let btnContent = "";
        let btnClass = "earn-btn";
        let extraStyle = "";
        
        if (status === 0) {
            btnContent = `<span>Перейти</span>`;
        } else if (status === 9) {
            btnClass += " reward-loading";
            extraStyle = "pointer-events: none;";
            btnContent = `<span class="eos-icons--loading"></span>`;
        } else if (status === 1) {
            btnClass += " reward-complete-btn";
            btnContent = `<svg xmlns="http://www.w3.org/2000/svg" color="#090909" fill="none" height="18px" viewBox="0 0 24 24" stroke-width="3" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5"/></svg>`;
        } else if (status === 2) {
            btnClass += " reward-fine-btn";
            btnContent = `<svg xmlns="http://www.w3.org/2000/svg" color="#ffffff" fill="none" height="18px" viewBox="0 0 24 24" stroke-width="3" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12"/></svg>`;
        }

        const rewardClass = status === 1 ? "reward-complete" : (status === 2 ? "reward-fine" : "");

        html += `
            <div class="earn-component-container" id="task-card-${escapeHtml(task.id)}">
                <div class="task-icon sponsor">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="#38bdf8">
                        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10s10-4.48 10-10S17.52 2 12 2m4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19c-.14.75-.42 1-.68 1.03c-.58.05-1.02-.38-1.58-.75c-.88-.58-1.38-.94-2.23-1.5c-.99-.65-.35-1.01.22-1.59c.15-.15 2.71-2.48 2.76-2.69a.2.2 0 0 0-.05-.18c-.06-.05-.14-.03-.21-.02c-.09.02-1.49.95-4.22 2.79c-.4.27-.76.41-1.08.4c-.36-.01-1.04-.2-1.55-.37c-.63-.2-1.12-.31-1.08-.66c.02-.18.27-.36.75-.55c2.92-1.27 4.86-2.11 5.83-2.51c2.78-1.16 3.35-1.36 3.73-1.36c.08 0 .27.02.39.12c.1.08.13.19.14.27c-.01.06.01.24 0 .38"/>
                    </svg>
                </div>
                <div class="task-item-info">
                    <span class="name">${escapeHtml(task.title)}</span>
                    <span class="reward ${rewardClass}">+${task.reward} ${escapeHtml(task.currency || 'MP')}</span>
                </div>
                <button type="button" class="${btnClass}" style="${extraStyle}" onclick="executeTask('${escapeHtml(task.id)}', '${escapeHtml(task.url)}')">
                    ${btnContent}
                </button>
            </div>
        `;
    });
    listEl.innerHTML = html;
}

async function executeTask(taskId, url) {
    if (activeTasksStatus[taskId] === 9 || activeTasksStatus[taskId] === 1) return;
    
    triggerHaptic("light");
    activeTasksStatus[taskId] = 9;
    renderTasksList(cachedTasksList);

    if (tg && typeof tg.openTelegramLink === "function" && url.includes("t.me")) {
        tg.openTelegramLink(url);
    } else {
        window.open(url, "_blank");
    }

    // Automatic 10-second check like in Nuxt
    setTimeout(async () => {
        try {
            const res = await apiRequest("/api/tasks/check", "POST", { task_id: taskId });
            if (res && res.success) {
                triggerHaptic("success");
                activeTasksStatus[taskId] = 1;
                if (typeof res.mp_balance !== "undefined") {
                    state.user.mp_balance = Number(res.mp_balance);
                    renderUserHeader();
                }
                renderTasksList(cachedTasksList);
                // After 2 seconds, smoothly remove the task
                setTimeout(() => {
                    const card = document.getElementById(`task-card-${taskId}`);
                    if (card) card.classList.add("task-removing");
                    setTimeout(() => {
                        activeTasksStatus[taskId] = "deleted";
                        loadTasks();
                    }, 400);
                }, 2000);
            } else {
                triggerHaptic("error");
                activeTasksStatus[taskId] = 2;
                showToast(res.error || "Подписка не найдена", "error");
                renderTasksList(cachedTasksList);
                setTimeout(() => {
                    activeTasksStatus[taskId] = 0;
                    renderTasksList(cachedTasksList);
                }, 2500);
            }
        } catch (err) {
            triggerHaptic("error");
            activeTasksStatus[taskId] = 2;
            showToast(err.message || "Подписка не найдена", "error");
            renderTasksList(cachedTasksList);
            setTimeout(() => {
                activeTasksStatus[taskId] = 0;
                renderTasksList(cachedTasksList);
            }, 2500);
        }
    }, 10000);
}

window.switchView = switchView;
window.placeArenaBet = placeArenaBet;
window.setQuickBet = setQuickBet;
window.multiplyBet = multiplyBet;
window.setMaxBet = setMaxBet;
window.openHistoryModal = openHistoryModal;
window.openInfoModal = openInfoModal;
window.openFairModal = openFairModal;
window.copyFairHash = copyFairHash;
window.calculateSha256InModal = calculateSha256InModal;
window.fillFairCalc = fillFairCalc;
window.closeModal = closeModal;
window.closeModalOnOverlay = closeModalOnOverlay;
window.openReplayModal = openReplayModal;
window.restartReplayAnimation = restartReplayAnimation;
window.loadTasks = loadTasks;
window.executeTask = executeTask;
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

    const startParam = tg?.initDataUnsafe?.start_param || "";
    if (startParam === "tasks" || startParam === "earn" || window.location.hash === "#tasks" || window.location.search.includes("tasks")) {
        switchView("tasks");
    }

    setInterval(pollActiveRound, 2000);
    setInterval(fetchUserProfile, 4000);
    setInterval(measurePing, 1000);
}
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initApp);
} else {
    initApp();
}