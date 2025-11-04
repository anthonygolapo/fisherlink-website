function showDelayWarning(sender, minutes) {
    const safeId = `delay-alert-${sender.replace(/[^a-zA-Z0-9]/g, "_")}`;
    if (document.getElementById(safeId)) return;

    const alertDiv = document.createElement("div");
    alertDiv.id = safeId;
    alertDiv.innerHTML = `⏰ <strong>${sender}</strong> has been inactive for ${Math.floor(minutes)} minutes!`;
    alertDiv.style.background = "#a46b00ff";
    alertDiv.style.color = "black";
    alertDiv.style.padding = "8px";
    alertDiv.style.margin = "100px";
    alertDiv.style.borderRadius = "5px";
    alertDiv.style.position = "fixed";
    alertDiv.style.right = "-70px"; // Right aligned
    alertDiv.style.zIndex = 1000; // Lower than modal/sidebar
    alertDiv.style.boxShadow = "0 0 10px rgba(0,0,0,0.3)";
    alertDiv.style.maxWidth = "400px"; // Optional: limit width

    // Calculate vertical stacking
    const existingAlerts = document.querySelectorAll("[id^='delay-alert-']");
    const offsetTop = existingAlerts.length * 60 + 10; // 60px per alert
    alertDiv.style.top = `${offsetTop}px`;

    document.body.appendChild(alertDiv);

    setTimeout(() => {
        alertDiv.remove();
        adjustPopupPositions(); // Optional: define if you want to close gaps
    }, 10000);
}


// Function to adjust the vertical positions of remaining popups
function adjustPopupPositions() {
    const remainingAlerts = document.querySelectorAll("[id^='delay-alert-']");
    remainingAlerts.forEach((alertDiv, index) => {
        alertDiv.style.top = `${index * 60 + 10}px`; // Recalculate position for each alert
    });
}