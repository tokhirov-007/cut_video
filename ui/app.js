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



    // Upload Submission
    const uploadForm = document.getElementById('upload-form');
    if (uploadForm) {
        uploadForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('upload-btn');
            const progress = document.getElementById('upload-progress');
            const resultDiv = document.getElementById('create-result');

            btn.disabled = true;
            progress.style.display = 'block';
            resultDiv.style.display = 'none';

            const fileInput = document.getElementById('upload-video');
            const file = fileInput.files[0];

            // Extract the actual date the video was created/modified from file metadata
            const fileDate = new Date(file.lastModified);
            const year = fileDate.getFullYear();
            const month = String(fileDate.getMonth() + 1).padStart(2, '0');
            const day = String(fileDate.getDate()).padStart(2, '0');
            const isoDate = `${year}-${month}-${day}`;

            const formData = new FormData();
            formData.append('date', isoDate);
            formData.append('video', file);

            const pollStatus = async (taskId) => {
                const interval = setInterval(async () => {
                    try {
                        const statusRes = await fetch(`${BASE_URL}/tasks/${taskId}`);
                        const statusData = await statusRes.json();
                        document.getElementById('progress-text').innerText = `Video kesilmoqda... Holati: ${statusData.status}`;
                        const currentStatus = statusData.status ? statusData.status.toUpperCase() : '';
                        if (currentStatus === 'COMPLETED') {
                            clearInterval(interval);
                            progress.style.display = 'none';
                            resultDiv.className = 'result-message success';
                            resultDiv.innerHTML = `Avtomatik kesish va AI ga yuborish muvaffaqiyatli yakunlandi!<br><br><b>Jarayonlar:</b><ul style="text-align:left; margin-top:10px;">${(statusData.logs || []).map(l => `<li style="font-size: 0.85rem">${l}</li>`).join('')}</ul>`;
                            resultDiv.style.display = 'block';
                            btn.disabled = false;
                        } else if (currentStatus === 'FAILED') {
                            clearInterval(interval);
                            progress.style.display = 'none';
                            resultDiv.className = 'result-message error';
                            const errorMsg = statusData.logs && statusData.logs.length > 0 
                                ? statusData.logs[statusData.logs.length - 1] 
                                : "Noma'lum xatolik yuz berdi.";
                            resultDiv.innerText = `Xatolik yuz berdi. Sababi: ${errorMsg}`;
                            resultDiv.style.display = 'block';
                            btn.disabled = false;
                        }
                    } catch (e) {
                        console.error('Polling xatosi', e);
                    }
                }, 3000);
            };

            try {
                const res = await fetch(`${BASE_URL}/upload-and-process`, {
                    method: 'POST',
                    body: formData
                });
                const data = await res.json();

                if (res.ok && data.task_id) {
                    pollStatus(data.task_id);
                    uploadForm.reset();
                } else if (res.ok) {
                    // No intervals found
                    progress.style.display = 'none';
                    resultDiv.className = 'result-message error';
                    resultDiv.innerText = data.message;
                    resultDiv.style.display = 'block';
                    btn.disabled = false;
                } else {
                    progress.style.display = 'none';
                    resultDiv.className = 'result-message error';
                    resultDiv.innerText = `Xatolik: ${data.detail || JSON.stringify(data)}`;
                    resultDiv.style.display = 'block';
                    btn.disabled = false;
                }
            } catch (err) {
                progress.style.display = 'none';
                resultDiv.className = 'result-message error';
                resultDiv.innerText = 'Server bilan aloqa uzildi.';
                resultDiv.style.display = 'block';
                btn.disabled = false;
            }
        });
    }

    // Status Check logic
    document.getElementById('check-status-btn').addEventListener('click', async () => {
        const dateStr = document.getElementById('status-date').value;
        const parts = dateStr.split('/');
        const date = parts.length === 3 ? `${parts[2]}-${parts[1]}-${parts[0]}` : dateStr;

        const container = document.getElementById('status-result');
        container.innerHTML = 'Loading...';

        if (!dateStr) {
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
        const dateStr = document.getElementById('download-date').value;
        const parts = dateStr.split('/');
        const date = parts.length === 3 ? `${parts[2]}-${parts[1]}-${parts[0]}` : dateStr;

        const room = document.getElementById('download-room').value.trim();
        const container = document.getElementById('downloads-result');
        container.innerHTML = 'Loading...';

        if (!dateStr || !room) {
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
