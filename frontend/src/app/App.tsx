import { useState } from "react";
import { Link, Outlet, useNavigate } from "react-router-dom";
import { Alert } from "../components/feedback/Alert";
import { Header } from "../components/layout/Header";
import { Navbar } from "../components/layout/Navbar";
import { ConfirmationModal } from "../components/modals/ConfirmationModal";
import { useAuth } from "./AuthContext";

export function App() {
  const { state, logout } = useAuth();
  const navigate = useNavigate();
  const [logoutPending, setLogoutPending] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const [logoutConfirmationOpen, setLogoutConfirmationOpen] = useState(false);
  const isAuthenticated = state.status === "authenticated";
  const isStudent =
    isAuthenticated &&
    (state.user.role === "student" || state.user.role === "student_admin");
  const isAdmin =
    isAuthenticated &&
    (state.user.role === "admin" || state.user.role === "student_admin");
  const isAdminOnly = isAuthenticated && state.user.role === "admin";

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
            ...(state.status === "authenticated"
              ? []
              : [{ href: "/", label: "Início" }]),
            ...(isStudent
              ? [
                  { href: "/student", label: "Painel" },
                  { href: "/student/survey", label: "Pesquisa" },
                  { href: "/student/proposals", label: "Propostas" },
                  { href: "/student/gallery", label: "Galeria e votação" },
                  { href: "/student/results", label: "Resultados" },
                ]
              : []),
            ...(isAdminOnly
              ? [
                  { href: "/student", label: "Painel da eleição" },
                  { href: "/student/gallery", label: "Propostas aprovadas" },
                ]
              : []),
            ...(isAdmin
              ? [{ href: "/admin", label: "Administração" }]
              : []),
            ...(state.status === "authenticated"
              ? []
              : [{ href: "/login", label: "Entrar" }]),
          ]}
          action={
            state.status === "authenticated"
              ? {
                  label: logoutPending ? "Saindo..." : "Sair",
                  onClick: () => setLogoutConfirmationOpen(true),
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
      <ConfirmationModal
        cancelLabel="Continuar conectado"
        confirmLabel={logoutPending ? "Saindo..." : "Sair"}
        isOpen={logoutConfirmationOpen}
        message="Deseja encerrar sua sessão no ThreeShirt?"
        onCancel={() => setLogoutConfirmationOpen(false)}
        onConfirm={() => {
          setLogoutConfirmationOpen(false);
          void handleLogout();
        }}
        title="Confirmar saída"
      />
      <footer className="site-footer">
        <p>ThreeShirt · <a href="https://github.com/Kevelincristine" target="_blank" rel="noopener noreferrer">GitHub da Criadora</a> </p>
      </footer>
    </>
  );
}
