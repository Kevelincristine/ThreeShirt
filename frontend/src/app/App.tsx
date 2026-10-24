import { useState } from "react";
import { Link, Outlet, useNavigate } from "react-router-dom";
import { Alert } from "../components/feedback/Alert";
import { Header } from "../components/layout/Header";
import { Navbar } from "../components/layout/Navbar";
import { useAuth } from "./AuthContext";

export function App() {
  const { state, logout } = useAuth();
  const navigate = useNavigate();
  const [logoutPending, setLogoutPending] = useState(false);
  const [logoutError, setLogoutError] = useState("");

  async function handleLogout(): Promise<void> {
    if (logoutPending) return;
    setLogoutPending(true);
    setLogoutError("");
    try {
      await logout();
      navigate("/login", { replace: true });
    } catch {
      setLogoutError("Não foi possível encerrar a sessão. Tente novamente.");
    } finally {
      setLogoutPending(false);
    }
  }

  return (
    <>
      <Header
        brand={
          <Link className="brand-link" to="/">
            ThreeShirt
          </Link>
        }
      >
        <Navbar
          links={[
            { href: "/", label: "Início" },
            ...(state.status === "authenticated" &&
            state.user.role === "student"
              ? [{ href: "/student", label: "Painel do estudante" }]
              : []),
            ...(state.status === "authenticated"
              ? []
              : [{ href: "/login", label: "Entrar" }]),
          ]}
          action={
            state.status === "authenticated"
              ? {
                  label: logoutPending ? "Saindo..." : "Sair",
                  onClick: () => void handleLogout(),
                  disabled: logoutPending,
                }
              : undefined
          }
        />
      </Header>
      <main className="app-main">
        {logoutError && <Alert variant="error">{logoutError}</Alert>}
        <Outlet />
      </main>
      <footer className="site-footer">
        <p>ThreeShirt · criação coletiva da turma</p>
      </footer>
    </>
  );
}
