console.log("Bus Booking App JS Loaded");

// Example: socket connection
const socket = io();
socket.on("connect", () => {
    console.log("Connected to server via Socket.IO:", socket.id);
});