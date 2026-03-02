const BASE_URL = window.location.origin;

document.addEventListener('DOMContentLoaded', () => {
    // Tab switching logic
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

            btn.classList.add('active');
            document.getElementById(btn.dataset.target).classList.add('active');
        });
    });

    // Dynamic Interval Rows
    const addIntervalBtn = document.getElementById('add-interval');
    const intervalsContainer = document.getElementById('intervals-container');

    function updateRemoveButtons() {
        const rows = intervalsContainer.querySelectorAll('.interval-row');
        rows.forEach(row => {
            const btn = row.querySelector('.remove-btn');
            btn.disabled = rows.length === 1;
        });
    }

    addIntervalBtn.addEventListener('click', () => {
        const row = document.createElement('div');
        row.className = 'interval-row';
        row.innerHTML = `
            <input type="time" class="int-start" required> -
            <input type="time" class="int-end" required>
            <button type="button" class="remove-btn">&times;</button>
        `;
        intervalsContainer.appendChild(row);

        row.querySelector('.remove-btn').addEventListener('click', () => {
            row.remove();
            updateRemoveButtons();
        });
        updateRemoveButtons();
    });

    intervalsContainer.querySelector('.remove-btn').addEventListener('click', function () {
        if (intervalsContainer.querySelectorAll('.interval-row').length > 1) {
            this.closest('.interval-row').remove();
            updateRemoveButtons();
        }
    });

    // Form submission
    const taskForm = document.getElementById('task-form');
    taskForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const date = document.getElementById('task-date').value;
        const rooms = document.getElementById('task-rooms').value.split(',').map(r => r.trim()).filter(r => r);
        const intervalRows = intervalsContainer.querySelectorAll('.interval-row');

        const intervals = Array.from(intervalRows).map(row => ({
            start: row.querySelector('.int-start').value,
            end: row.querySelector('.int-end').value
        }));

        const payload = { date, rooms, intervals };
        const resultDiv = document.getElementById('create-result');

        try {
            const res = await fetch(`${BASE_URL}/process-day`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();

            if (res.ok) {
                resultDiv.className = 'result-message success';
                resultDiv.innerText = `Success! Tasks started. IDs: ${data.task_ids.join(', ')}`;
                setTimeout(() => { resultDiv.style.display = 'none'; resultDiv.className = 'result-message'; }, 5000);
            } else {
                resultDiv.className = 'result-message error';
                resultDiv.innerText = `Error: ${JSON.stringify(data)}`;
            }
        } catch (err) {
            resultDiv.className = 'result-message error';
            resultDiv.innerText = 'Network error fetching data.';
        }
    });

    // Status Check logic
    document.getElementById('check-status-btn').addEventListener('click', async () => {
        const date = document.getElementById('status-date').value;
        const container = document.getElementById('status-result');
        container.innerHTML = 'Loading...';

        if (!date) {
            container.innerHTML = '<span style="color:red">Please select a date</span>';
            return;
        }

        try {
            const res = await fetch(`${BASE_URL}/status/${date}`);
            const data = await res.json();

            if (data.length === 0) {
                container.innerHTML = 'No tasks found for this date.';
                return;
            }

            container.innerHTML = data.map(t => `
                <div class="status-card">
                    <div>
                        <strong>Room: ${t.room}</strong>
                        <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 0.3rem">${t.updated_at || 'Just now'}</div>
                    </div>
                    <span class="status-badge ${t.status}">${t.status.toUpperCase()}</span>
                </div>
            `).join('');
        } catch (e) {
            container.innerHTML = '<span style="color:red">Failed to fetch status</span>';
        }
    });

    // Download Videos logic
    document.getElementById('load-videos-btn').addEventListener('click', async () => {
        const date = document.getElementById('download-date').value;
        const room = document.getElementById('download-room').value.trim();
        const container = document.getElementById('downloads-result');
        container.innerHTML = 'Loading...';

        if (!date || !room) {
            container.innerHTML = '<span style="color:red">Please fill both fields</span>';
            return;
        }

        try {
            const res = await fetch(`${BASE_URL}/videos/${date}/${room}`);
            const data = await res.json();

            if (!data || data.length === 0) {
                container.innerHTML = 'No videos found ready to download.';
                return;
            }

            container.innerHTML = data.map(filename => `
                <div class="video-card">
                    <span style="word-break: break-all; margin-right: 15px;">${filename}</span>
                    <a href="${BASE_URL}/download/${date}/${room}/${filename}" class="download-btn" download>Download</a>
                </div>
            `).join('');
        } catch (e) {
            container.innerHTML = '<span style="color:red">Failed to fetch videos</span>';
        }
    });
});
