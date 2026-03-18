function getNextUrl() {
  const params = new URLSearchParams(window.location.search);
  return params.get("next") || "/";
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("login-form");
  const status = document.getElementById("login-status");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    status.textContent = "Проверяю доступ...";
    status.classList.remove("error", "success");

    const payload = {
      username: document.getElementById("login-username").value.trim(),
      password: document.getElementById("login-password").value,
    };

    try {
      const response = await fetch("/auth/login", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: "Ошибка входа." }));
        throw new Error(error.detail || "Ошибка входа.");
      }

      status.textContent = "Вход выполнен, перенаправляю...";
      status.classList.add("success");
      window.location.href = getNextUrl();
    } catch (error) {
      status.textContent = error.message || "Не удалось выполнить вход.";
      status.classList.add("error");
    }
  });
});
