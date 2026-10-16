// The browser talks to the gateway for queue and purchase actions.
const API_BASE = 'http://localhost:8000';
const INVENTORY_BASE = 'http://localhost:8001';

// Keep tokens in browser storage so a page refresh does not lose the user's queue view.
let tokens = [];
let statusCheckInterval = null;

function isValidToken(token) {
    return typeof token === 'string'
        && token.length > 0
        && token !== 'undefined'
        && token !== 'null';
}

// Start the dashboard refreshers when the page is ready.
window.onload = () => {
    loadTokensFromStorage();
    refreshStats();
    setInterval(refreshStats, 2000);
    startStatusCheckLoop();
};

function loadTokensFromStorage() {
    const stored = localStorage.getItem('flashSaleTokens');
    if (!stored) {
        return;
    }

    try {
        const parsed = JSON.parse(stored);
        const parsedTokens = Array.isArray(parsed) ? parsed : [];
        tokens = parsedTokens.filter(
            token => token && isValidToken(token.token)
        );
        if (tokens.length !== parsedTokens.length || !Array.isArray(parsed)) {
            console.log(`Cleaned ${parsedTokens.length - tokens.length} invalid tokens from storage`);
            saveTokensToStorage();
        }
        renderTokenList();
    } catch (error) {
        console.error('Failed to load tokens from storage:', error);
        tokens = [];
        localStorage.removeItem('flashSaleTokens');
    }
}

function saveTokensToStorage() {
    localStorage.setItem('flashSaleTokens', JSON.stringify(tokens));
}

// Add a short message to the activity log and keep the newest 50 messages.
function log(message, type = 'info') {
    const logDiv = document.getElementById('activityLog');
    const entry = document.createElement('div');
    entry.className = 'log-entry';

    const time = new Date().toLocaleTimeString();
    const typeClass = type === 'success' ? 'log-success'
        : type === 'error' ? 'log-error'
        : 'log-info';

    entry.innerHTML = `<span class="log-time">[${time}]</span> <span class="${typeClass}">${message}</span>`;
    logDiv.insertBefore(entry, logDiv.firstChild);

    while (logDiv.children.length > 50) {
        logDiv.removeChild(logDiv.lastChild);
    }
}

// Load current stock and queue size for the dashboard.
async function refreshStats() {
    try {
        const stockRes = await fetch(`${INVENTORY_BASE}/stock/phone`);
        const stockData = await stockRes.json();
        document.getElementById('stock').textContent = stockData.stock;

        const queueRes = await fetch('http://localhost:8004/queue/phone/length');
        const queueData = await queueRes.json();
        document.getElementById('queue').textContent = queueData.waiting;

        const orderCount = 100 - stockData.stock;
        document.getElementById('orders').textContent = orderCount > 0 ? orderCount : 0;
    } catch (error) {
        console.error('Stats refresh failed:', error);
    }
}

// Submit a new customer to the gateway waiting room.
async function joinQueue() {
    const userId = document.getElementById('userId').value;
    const productId = document.getElementById('productId').value;
    const joinButton = document.querySelector('button[onclick="joinQueue()"]');

    if (!userId) {
        alert('Please enter a user ID');
        return;
    }

    if (joinButton) {
        joinButton.disabled = true;
        joinButton.textContent = 'Joining...';
    }

    try {
        const response = await fetch(
            `${API_BASE}/queue/join?user_id=${encodeURIComponent(userId)}&product_id=${encodeURIComponent(productId)}`,
            { method: 'POST' }
        );

        if (response.status === 410) {
            const error = await response.json();
            const message = error.detail || 'Product is sold out';
            log(`✗ ${message}`, 'error');
            return;
        }

        if (!response.ok) {
            try {
                const error = await response.json();
                log(`✗ Failed to join queue: ${error.detail || 'Unknown error'}`, 'error');
            } catch {
                log(`✗ Failed to join queue: HTTP ${response.status}`, 'error');
            }
            return;
        }

        const data = await response.json();

        if (!data || !isValidToken(data.token)) {
            console.error('Invalid response data:', data);
            log('✗ Invalid response from server', 'error');
            return;
        }

        const existingTokenIndex = tokens.findIndex(t => t && t.token === data.token);
        
        if (existingTokenIndex !== -1) {
            tokens[existingTokenIndex].position = data.position;
            tokens[existingTokenIndex].status = data.status || 'waiting';
            log(`ℹ Already in queue as ${userId}`, 'info');
        } else {
            tokens.push({
                token: data.token,
                userId,
                productId,
                status: data.status || 'waiting',
                position: data.position !== undefined ? data.position : null,
                joinedAt: new Date().toISOString()
            });
            const positionText = data.position !== undefined ? `position: ${data.position}` : '';
            log(`✓ Joined queue as ${userId} ${positionText}`, 'success');
        }

        tokens = tokens.filter(token => token && isValidToken(token.token));
        saveTokensToStorage();
        renderTokenList();
    } catch (error) {
        log(`✗ Failed to join queue: ${error.message}`, 'error');
    } finally {
        if (joinButton) {
            joinButton.disabled = false;
            joinButton.textContent = 'Join Queue';
        }
    }
}

// Check each token regularly so admitted users can complete their purchase.
function startStatusCheckLoop() {
    if (statusCheckInterval) {
        clearInterval(statusCheckInterval);
    }
    statusCheckInterval = setInterval(checkAllStatuses, 2000);
}

async function checkAllStatuses() {
    if (tokens.length === 0) {
        return;
    }

    for (const tokenObj of tokens) {
        if (!tokenObj || !isValidToken(tokenObj.token)) {
            console.warn('Skipping invalid token:', tokenObj);
            continue;
        }

        if (tokenObj.status === 'completed') {
            continue;
        }

        try {
            const response = await fetch(`${API_BASE}/queue/status/${tokenObj.token}`);
            
            // If token not found (404), remove it from local storage
            if (response.status === 404) {
                console.log(`Token ${tokenObj.token.substring(0, 8)} not found, removing from list`);
                tokens = tokens.filter(t => t.token !== tokenObj.token);
                saveTokensToStorage();
                renderTokenList();
                continue;
            }

            if (!response.ok) {
                continue;
            }
            
            const data = await response.json();

            if (data.status !== tokenObj.status) {
                tokenObj.status = data.status;
                tokenObj.position = data.position;

                if (data.status === 'admitted') {
                    log(`✓ Token ${tokenObj.token.substring(0, 8)}... admitted!`, 'success');
                }

                saveTokensToStorage();
                renderTokenList();
            }
        } catch (error) {
            console.error(`Status check failed for ${tokenObj.token}:`, error);
        }
    }
}

// Ask the gateway to reserve stock and create an order for an admitted token.
async function purchase(token) {
    if (!isValidToken(token)) {
        log('✗ Invalid token', 'error');
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/purchase?token=${encodeURIComponent(token)}`, {
            method: 'POST'
        });
        const data = await response.json();

        if (data.success) {
            const tokenObj = tokens.find((item) => item.token === token);
            if (tokenObj) {
                tokenObj.status = 'completed';
                tokenObj.orderId = data.order.id;
                saveTokensToStorage();
                renderTokenList();
            }

            log(`✓ Purchase successful! Order ID: ${data.order.id}`, 'success');
            refreshStats();
        } else {
            log(`✗ Purchase failed: ${data.reason || 'Unknown error'}`, 'error');
        }
    } catch (error) {
        log(`✗ Purchase error: ${error.message}`, 'error');
    }
}

function removeToken(token) {
    if (!isValidToken(token)) {
        console.error('Invalid token passed to removeToken:', token);
        tokens = tokens.filter(item => item && item.token !== token);
        saveTokensToStorage();
        renderTokenList();
        return;
    }

    tokens = tokens.filter((item) => item.token !== token);
    saveTokensToStorage();
    renderTokenList();
    log(`Removed token ${token.substring(0, 8)}...`, 'info');
}

function clearAllTokens() {
    if (confirm('Clear all tokens?')) {
        tokens = [];
        saveTokensToStorage();
        renderTokenList();
        log('All tokens cleared', 'info');
    }
}

// Render tokens in a stable order: admitted, waiting, then completed.
function renderTokenList() {
    const listDiv = document.getElementById('tokenList');

    tokens = tokens.filter(token => token && isValidToken(token.token));
    document.getElementById('tokenCount').textContent = tokens.length;

    if (tokens.length === 0) {
        listDiv.innerHTML = `
            <p style="color: #94a3b8; text-align: center; padding: 20px;">
                Join the queue to see your tokens here
            </p>
        `;
        return;
    }

    const sorted = [...tokens].sort((a, b) => {
        const order = { 'admitted': 0, 'waiting': 1, 'completed': 2 };
        const statusA = a.status || 'waiting';
        const statusB = b.status || 'waiting';
        return order[statusA] - order[statusB];
    });

    listDiv.innerHTML = sorted.map(t => {
        if (!t || !isValidToken(t.token)) {
            return '';
        }
        if (!t || !t.token) {
            return '';
        }

        const status = t.status || 'waiting';
        const statusText = status.toUpperCase();
        let buttons;

        if (t.status === 'admitted') {
            buttons = `
                <div class="button-group">
                    <button class="small" onclick="purchase('${t.token}')">Complete Purchase</button>
                    <button class="small secondary" onclick="removeToken('${t.token}')">Remove</button>
                </div>`;
        } else if (t.status === 'completed') {
            buttons = `
                <div class="button-group">
                    <button class="small secondary" onclick="removeToken('${t.token}')">Remove</button>
                </div>
                <p class="purchase-result">✓ Order ID: ${t.orderId}</p>`;
        } else {
            buttons = `
                <div class="button-group">
                    <button class="small secondary" onclick="removeToken('${t.token}')">Remove</button>
                </div>
                ${t.position !== undefined && t.position !== null
                    ? `<p class="queue-position">Queue Position: ${t.position}</p>` : ''}`;
        }

        return `
            <div class="token-item ${t.status}">
                <div class="token-header">
                    <span class="token-id">${t.token}</span>
                    <span class="status ${t.status}">${statusText}</span>
                </div>
                <div class="token-body">
                    <p class="token-details">User: ${t.userId} | Product: ${t.productId}</p>
                    ${buttons}
                </div>
            </div>`;
    }).filter(html => html !== '').join('');
}

// Set the demo product stock through the inventory service.
async function setStock() {
    const amount = document.getElementById('stockAmount').value;

    if (!amount || amount < 0) {
        alert('Please enter a valid stock amount');
        return;
    }

    try {
        const response = await fetch(`${INVENTORY_BASE}/stock/phone/${amount}`, { method: 'POST' });
        const data = await response.json();
        log(`✓ Stock set to ${data.stock}`, 'success');
        refreshStats();
    } catch (error) {
        log(`✗ Failed to set stock: ${error.message}`, 'error');
    }
}