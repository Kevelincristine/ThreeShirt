import React from "react";
import ReactDOM from "react-dom/client";
import { AuthProvider } from "./app/AuthContext";
import { RouterProvider } from "react-router-dom";
import { router } from "./app/routes";
import "./styles/tokens.css";
import "./styles/global.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <RouterProvider router={router} />
    </AuthProvider>
  </React.StrictMode>,
);
