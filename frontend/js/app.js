// The browser talks to the gateway for queue and purchase actions.
const API_BASE = 'http://localhost:8000';
const INVENTORY_BASE = 'http://localhost:8001';

// Keep tokens in browser storage so a page refresh does not lose the user's queue view.
let tokens = [];
let statusCheckInterval = null;

// Start the dashboard refreshers when the page is ready.
window.onload = () => {
    loadTokensFromStorage();
    refreshStats();
    setInterval(refreshStats, 2000);
    startStatusCheckLoop();
};

function loadTokensFromStorage() {
    const stored = localStorage.getItem('flashSaleTokens');
    if (stored) {
        tokens = JSON.parse(stored);
        renderTokenList();
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

    if (!userId) {
        alert('Please enter a user ID');
        return;
    }

    try {
        const response = await fetch(
            `${API_BASE}/queue/join?user_id=${encodeURIComponent(userId)}&product_id=${encodeURIComponent(productId)}`,
            { method: 'POST' }
        );
        const data = await response.json();

        tokens.push({
            token: data.token,
            userId,
            productId,
            status: 'waiting',
            position: data.position,
            joinedAt: new Date().toISOString()
        });

        saveTokensToStorage();
        renderTokenList();
        log(`✓ Joined queue as ${userId} (position: ${data.position})`, 'success');
    } catch (error) {
        log(`✗ Failed to join queue: ${error.message}`, 'error');
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
        if (tokenObj.status === 'completed') {
            continue;
        }

        try {
            const response = await fetch(`${API_BASE}/queue/status/${tokenObj.token}`);
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
    document.getElementById('tokenCount').textContent = tokens.length;

    if (tokens.length === 0) {
        listDiv.innerHTML = '<p class="empty-message">Join the queue to see your tokens here</p>';
        return;
    }

    const sorted = [...tokens].sort((a, b) => {
        const order = { admitted: 0, waiting: 1, completed: 2 };
        return order[a.status] - order[b.status];
    });

    listDiv.innerHTML = sorted.map((tokenObj) => {
        const statusText = tokenObj.status.toUpperCase();
        let buttons;

        if (tokenObj.status === 'admitted') {
            buttons = `
                <div class="button-group">
                    <button class="small" onclick="purchase('${tokenObj.token}')">Complete Purchase</button>
                    <button class="small secondary" onclick="removeToken('${tokenObj.token}')">Remove</button>
                </div>`;
        } else if (tokenObj.status === 'completed') {
            buttons = `
                <div class="button-group">
                    <button class="small secondary" onclick="removeToken('${tokenObj.token}')">Remove</button>
                </div>
                <p class="purchase-result">✓ Order ID: ${tokenObj.orderId}</p>`;
        } else {
            buttons = `
                <div class="button-group">
                    <button class="small secondary" onclick="removeToken('${tokenObj.token}')">Remove</button>
                </div>
                ${tokenObj.position !== undefined
                    ? `<p class="queue-position">Queue Position: ${tokenObj.position}</p>` : ''}`;
        }

        return `
            <div class="token-item ${tokenObj.status}">
                <div class="token-header">
                    <span class="token-id">${tokenObj.token}</span>
                    <span class="status ${tokenObj.status}">${statusText}</span>
                </div>
                <div class="token-body">
                    <p class="token-details">User: ${tokenObj.userId} | Product: ${tokenObj.productId}</p>
                    ${buttons}
                </div>
            </div>`;
    }).join('');
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
