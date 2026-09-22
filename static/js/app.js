const byId = (id) => document.getElementById(id);
const getActiveFarmId = () => {
    try { return JSON.parse(localStorage.getItem("activeFarm") || "null")?.id || null; }
    catch (error) { return null; }
};

// ─── IRRIGATION FORM ──────────────────────────────────────────────────────────
const farmForm = byId("farmForm");
if (farmForm) {
    farmForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = farmForm.querySelector("button[type='submit']");
        const data = {
            farm_id: getActiveFarmId(),
            location: byId("location")?.value || "Unknown",
            crop: byId("crop").value,
            soil: byId("soil").value,
            moisture: Number(byId("moisture").value),
            temperature: Number(byId("temperature").value),
            humidity: Number(byId("humidity").value),
            rainfall: Number(byId("rainfall").value),
            farm_size: Number(byId("farmSize").value),
            water: byId("water").value
        };
        button.disabled = true;
        button.textContent = "Analyzing...";
        try {
            const response = await fetch("/api/irrigation", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
            const result = await response.json();
            if (!result.success) throw new Error(result.error || "Analysis failed");
            const recommendation = byId("recommendation");
            if (recommendation) recommendation.innerHTML = `<div class="recommendation-icon"><i class="fa-solid fa-droplet"></i></div><h2>${result.irrigation_required ? "Irrigation required" : "Irrigation not required"}</h2><p>${result.explanation}</p><div class="result-details"><span>Time<strong>${result.recommended_time}</strong></span><span>Duration<strong>${result.duration} min</strong></span><span>Water<strong>${result.water_quantity} L</strong></span><span>Risk<strong>${result.risk}</strong></span></div>`;
            ["moistureValue", "temperatureValue", "humidityValue", "rainValue"].forEach((id, index) => {
                const element = byId(id);
                if (element) element.textContent = `${[data.moisture, data.temperature, data.humidity, data.rainfall][index]}${index === 1 ? " C" : "%"}`;
            });
            const health = Math.max(0, 100 - (data.moisture < 30 ? 10 : 0) - (data.temperature > 32 ? 8 : 0) - (data.humidity < 35 ? 5 : 0) - (data.rainfall > 70 ? 5 : 0));
            if (byId("healthScore")) byId("healthScore").textContent = `${health}%`;
            if (byId("selectedCrop")) byId("selectedCrop").textContent = data.crop;
            if (byId("selectedSoil")) byId("selectedSoil").textContent = data.soil;
            if (byId("selectedWater")) byId("selectedWater").textContent = data.water;

            // Save context for AI Assistant
            localStorage.setItem("farmContext", JSON.stringify({
                farm_id: result.farm_id || getActiveFarmId(),
                farm_name: result.farm_name || "",
                crop: data.crop,
                soil: data.soil,
                moisture: data.moisture,
                temperature: data.temperature,
                humidity: data.humidity,
                rainfall: data.rainfall,
                farm_size: data.farm_size,
                water: data.water,
                irrigation_required: result.irrigation_required ? "Yes" : "No",
                health: health
            }));
        } catch (error) {
            alert(error.message || "Unable to analyze the farm.");
        } finally {
            button.disabled = false;
            button.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> Analyze farm';
        }
    });
}

// ─── WEATHER BUTTON (dashboard widget) ────────────────────────────────────────
const weatherButton = byId("getWeatherBtn");
if (weatherButton) weatherButton.addEventListener("click", async () => {
    const location = byId("weatherLocationInput")?.value.trim() || byId("location")?.value.trim();
    if (!location) return alert("Enter a farm location first.");
    weatherButton.disabled = true;
    try {
        const farmQuery = getActiveFarmId() ? `&farm_id=${getActiveFarmId()}` : "";
        const response = await fetch(`/api/weather?location=${encodeURIComponent(location)}${farmQuery}`);
        const data = await response.json();
        if (!data.success) throw new Error(data.error || "Weather unavailable");
        if (byId("weatherLocation")) byId("weatherLocation").textContent = `${data.location}, ${data.country}`;
        if (byId("weatherTemp")) byId("weatherTemp").textContent = `${data.temperature} °C`;
        if (byId("weatherHumidity")) byId("weatherHumidity").textContent = `${data.humidity}%`;
        if (byId("weatherRain")) byId("weatherRain").textContent = `${data.rain_probability}%`;
        if (byId("weatherRainfall")) byId("weatherRainfall").textContent = `${data.rainfall} mm`;
        if (byId("rainfall")) byId("rainfall").value = data.rain_probability;
        if (byId("weatherAdvice")) byId("weatherAdvice").textContent = data.rain_probability > 40 ? "Rain is likely. Consider delaying irrigation." : "Check soil moisture before irrigating.";
    } catch (error) {
        alert(error.message || "Weather service unavailable.");
    } finally {
        weatherButton.disabled = false;
    }
});

// ─── AI CHATBOT ───────────────────────────────────────────────────────────────
// The chat only runs on pages that have #aiChatMessages (ai_assistant.html).
// It does NOT run on other pages to avoid conflicts.

const aiChatMessages = byId("aiChatMessages");
const aiChatInput    = byId("aiChatInput");
const aiSendBtn      = byId("aiSendMessageBtn");
const clearChatBtn   = byId("clearChatBtn");

if (aiChatMessages && aiChatInput && aiSendBtn) {

    // ── send on button click ──
    aiSendBtn.addEventListener("click", sendChatMessage);

    // ── send on Enter key ──
    aiChatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendChatMessage();
        }
    });

    // ── clear chat ──
    if (clearChatBtn) {
        clearChatBtn.addEventListener("click", () => {
            aiChatMessages.innerHTML = "";
            appendBotMessage("Chat cleared. How can I help you with your farm today?");
        });
    }
}

async function sendChatMessage() {
    if (!aiChatInput || !aiChatMessages) return;

    const message = aiChatInput.value.trim();
    if (!message) return;

    // Show user bubble
    appendUserMessage(message);
    aiChatInput.value = "";
    aiSendBtn.disabled = true;

    // Show typing indicator
    const typingId = appendTypingIndicator();

    // Load farm context from localStorage
    let context = {};
    try {
        const stored = localStorage.getItem("farmContext");
        if (stored) context = JSON.parse(stored);
    } catch (e) { /* no context stored yet */ }
    context.farm_id = context.farm_id || getActiveFarmId();

    try {
        const response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message, context })
        });

        removeTypingIndicator(typingId);

        const data = await response.json();

        if (data.success && data.reply) {
            appendBotMessage(data.reply);
        } else {
            // Show the actual server error so it's debuggable
            const errText = data.error || "The AI could not process your question. Please try again.";
            appendBotMessage(`⚠️ ${errText}`);
        }

    } catch (err) {
        removeTypingIndicator(typingId);
        appendBotMessage("⚠️ Could not connect to the AI service. Please check your internet connection and try again.");
    } finally {
        aiSendBtn.disabled = false;
        aiChatInput.focus();
    }
}

// Append a bot message bubble (avatar + content)
function appendBotMessage(text) {
    if (!aiChatMessages) return;
    const wrapper = document.createElement("div");
    wrapper.className = "bot-message";
    wrapper.innerHTML = `
        <div class="message-avatar">AI</div>
        <div class="message-content"><p>${escapeHtml(text)}</p></div>
    `;
    aiChatMessages.appendChild(wrapper);
    aiChatMessages.scrollTop = aiChatMessages.scrollHeight;
}

// Append a user message bubble
function appendUserMessage(text) {
    if (!aiChatMessages) return;
    const wrapper = document.createElement("div");
    wrapper.className = "user-message";
    const p = document.createElement("p");
    p.textContent = text;
    wrapper.appendChild(p);
    aiChatMessages.appendChild(wrapper);
    aiChatMessages.scrollTop = aiChatMessages.scrollHeight;
}

// Append animated typing indicator, return its unique ID
function appendTypingIndicator() {
    if (!aiChatMessages) return null;
    const id = "typing-" + Date.now();
    const wrapper = document.createElement("div");
    wrapper.className = "bot-message";
    wrapper.id = id;
    wrapper.innerHTML = `
        <div class="message-avatar">AI</div>
        <div class="message-content typing-indicator">
            <span></span><span></span><span></span>
        </div>
    `;
    aiChatMessages.appendChild(wrapper);
    aiChatMessages.scrollTop = aiChatMessages.scrollHeight;
    return id;
}

// Remove typing indicator by its unique ID
function removeTypingIndicator(id) {
    if (!id) return;
    const el = document.getElementById(id);
    if (el) el.remove();
}

// Safe HTML escape
function escapeHtml(str) {
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;")
        .replace(/\n/g, "<br>");
}

// ── Quick suggestion buttons (called directly from HTML onclick) ──
function askSuggestion(button) {
    if (!aiChatInput) return;
    aiChatInput.value = button.textContent.trim();
    sendChatMessage();
}
