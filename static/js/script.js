document.addEventListener('DOMContentLoaded', () => {
    console.log("Cainiao Tracking script loaded.");
    const form = document.getElementById('add-tracking-form');
    const trackingDetailsDiv = document.getElementById('tracking-details');
    const trackingIdDisplay = document.getElementById('tracking-id-display');
    const itemNameDisplay = document.getElementById('item-name-display');
    const statusDisplay = document.getElementById('status-display');
    const dateDisplay = document.getElementById('date-display');
    const historyList = document.getElementById('history-list');
    const mapDiv = document.getElementById('map');
    const progressBarDiv = document.getElementById('progress-bar'); // Progress bar container
    let map = null; // 地図オブジェクトを保持する変数
    let marker = null; // マーカーオブジェクトを保持する変数

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const formData = new FormData(form);
        const trackingNumber = formData.get('tracking_number');
        const itemName = formData.get('item_name');

        try {
            // バックエンドAPIに登録リクエストを送信
            const response = await fetch('/api/track', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ tracking_number: trackingNumber, item_name: itemName }),
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || '登録に失敗しました');
            }

            const result = await response.json();
            console.log('登録成功:', result);

            // 登録成功後、最新情報を取得して表示
            fetchTrackingInfo(trackingNumber);

            form.reset(); // フォームをリセット

        } catch (error) {
            console.error('登録エラー:', error);
            trackingDetailsDiv.innerHTML = `<p style="color: red;">エラー: ${error.message}</p>`;
        }
    });

    async function fetchTrackingInfo(trackingNumber) {
        try {
            const response = await fetch(`/api/track/${trackingNumber}`);
            if (!response.ok) {
                 const errorData = await response.json();
                throw new Error(errorData.error || '情報の取得に失敗しました');
            }
            const data = await response.json();
            console.log('取得成功:', data);
            displayTrackingInfo(data);
        } catch (error) {
            console.error('取得エラー:', error);
            trackingDetailsDiv.innerHTML = `<p style="color: red;">エラー: ${error.message}</p>`;
        }
    }

    function displayTrackingInfo(data) {
        trackingDetailsDiv.innerHTML = ''; // 初期メッセージをクリア

        trackingIdDisplay.textContent = data.tracking_number;
        itemNameDisplay.textContent = data.item_name;
        statusDisplay.textContent = data.status;
        statusDisplay.className = `status-display ${data.status_class}`; // Add base class + status class

        // 発送日と経過日数を計算して表示 (historyの最初のイベントを発送日と仮定)
        let shippingDateStr = data.history.length > 0 ? data.history[0].timestamp : data.last_updated;
        try {
            const shippingDate = new Date(shippingDateStr.replace(/-/g, '/')); // Safari compatibility
            const today = new Date();
            const elapsedDays = Math.floor((today - shippingDate) / (1000 * 60 * 60 * 24));
            dateDisplay.textContent = `${shippingDate.toLocaleDateString()} (${elapsedDays}日経過)`;
        } catch (e) {
             console.error("Date parsing error:", e);
             dateDisplay.textContent = shippingDateStr; // Fallback to original string
        }

        historyList.innerHTML = ''; // 履歴をクリア
        data.history.forEach(item => {
            const li = document.createElement('li');
            li.textContent = `${item.timestamp} - ${item.location}: ${item.description}`;
            historyList.appendChild(li);
        });

        // 進捗バーの更新
        updateProgressBar(data.status);

        // 地図表示の更新
        updateMap(data.current_location);
        console.log('Current location for map:', data.current_location);
    }

    // 地図初期化関数
    function initializeMap() {
        // 地図が既に初期化されている場合は何もしない
        if (map) return;

        // 地図表示エリアのデフォルトメッセージを削除
        const mapPlaceholder = mapDiv.querySelector('p');
        if (mapPlaceholder) {
            mapDiv.removeChild(mapPlaceholder);
        }


        map = L.map('map').setView([35.6895, 139.6917], 5); // 初期表示: 東京、ズームレベル5

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '© OpenStreetMap contributors'
        }).addTo(map);
    }

    // 地図更新関数
    function updateMap(location) {
        if (!map) {
            console.warn("Map not initialized yet.");
            return;
        }
        if (!location || typeof location.lat !== 'number' || typeof location.lng !== 'number') {
            console.warn("Invalid location data:", location);
             // マーカーが存在すれば削除
            if (marker) {
                map.removeLayer(marker);
                marker = null;
            }
            return;
        }

        const latLng = [location.lat, location.lng];

        // マーカーが既にあれば位置を更新、なければ新規作成
        if (marker) {
            marker.setLatLng(latLng);
        } else {
            marker = L.marker(latLng).addTo(map);
        }

        // マーカーの位置に地図の中心を移動
        map.setView(latLng, 13); // ズームレベル13に設定
    }

    // 進捗バー更新関数
    function updateProgressBar(currentStatus) {
        const steps = ['未発送', '発送中', '輸送中', '配達中', '配達済']; // 想定されるステータス順
        const locations = ['登録', '発送地', '経由地', '配達地域', '配達完了']; // 表示名

        progressBarDiv.innerHTML = ''; // Clear previous bar
        const lineFill = document.createElement('div'); // Create line fill element
        lineFill.className = 'progress-line-fill';
        progressBarDiv.appendChild(lineFill);


        let currentStepIndex = steps.indexOf(currentStatus);
        if (currentStepIndex === -1 && currentStatus !== '不明') {
             currentStepIndex = 0; // Default to first step if status unknown but not explicitly "不明"
        }

        steps.forEach((step, index) => {
            const stepDiv = document.createElement('div');
            stepDiv.className = 'progress-step';

            const locationNameSpan = document.createElement('span');
            locationNameSpan.className = 'location-name';
            // Use specific location names if available, otherwise use generic names
            locationNameSpan.textContent = locations[index] || step;
            stepDiv.appendChild(locationNameSpan);


            const dotSpan = document.createElement('span');
            dotSpan.className = 'dot';
            stepDiv.appendChild(dotSpan);


            if (index < currentStepIndex) {
                stepDiv.classList.add('completed');
            } else if (index === currentStepIndex) {
                stepDiv.classList.add('active');
            }

             // Handle '不明' status - no steps active/completed
            if (currentStatus === '不明') {
                 stepDiv.classList.remove('active', 'completed');
            }


            progressBarDiv.appendChild(stepDiv);
        });

         // Update progress line width
        let progressPercentage = 0;
        if (currentStepIndex > 0) {
            // Calculate percentage based on completed steps
            // The line connects centers, so it spans (steps.length - 1) segments.
            progressPercentage = (currentStepIndex / (steps.length - 1)) * 100;
        }
         if (currentStatus === '配達済') {
             progressPercentage = 100;
         }
         // Ensure percentage does not exceed 100
         progressPercentage = Math.min(progressPercentage, 100);

         // Adjust width calculation slightly to align better visually if needed
         // Example: Start line slightly after first dot, end slightly before last
         const startOffsetPercent = 10; // Matches CSS ::before left
         const endOffsetPercent = 10;   // Matches CSS ::before right
         const effectiveWidthPercent = 100 - startOffsetPercent - endOffsetPercent;
         const lineWidthPercent = (progressPercentage / 100) * effectiveWidthPercent;

         // Apply width (relative to the full bar width, considering offsets)
         // We set the width of the fill element itself.
         // If the line starts at 10% and ends at 90%, its total span is 80%.
         // A 50% progress means filling half of that 80% span = 40% of the total container width.
         // So, width = startOffset + (progressPercentage/100 * effectiveWidth)
         lineFill.style.width = `calc(${startOffsetPercent}% + ${lineWidthPercent}%)`;

         // Special case for 0% progress
         if (progressPercentage === 0) {
             lineFill.style.width = `${startOffsetPercent}%`; // Or just 0% if line shouldn't show at all initially
         }
          // Special case for 100% progress
         if (progressPercentage === 100) {
             lineFill.style.width = `${100 - endOffsetPercent}%`; // Fill up to the end offset
         }


    }


    initializeMap(); // ページ読み込み時に地図を初期化
});