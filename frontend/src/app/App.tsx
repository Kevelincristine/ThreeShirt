import { Link, Outlet } from "react-router-dom";
import { Header } from "../components/layout/Header";
import { Navbar } from "../components/layout/Navbar";

export function App() {
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
            { href: "/login", label: "Entrar" },
          ]}
        />
      </Header>
      <main className="app-main">
        <Outlet />
      </main>
      <footer className="site-footer">
        <p>ThreeShirt · login do estudante</p>
      </footer>
    </>
  );
}
