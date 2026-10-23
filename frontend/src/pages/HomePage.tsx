import { Alert } from "../components/feedback/Alert";
import { Link } from "react-router-dom";

export function HomePage() {
  return (
    <section aria-labelledby="home-title" className="paper-panel">
      <p className="eyebrow">ThreeShirt · um projeto da turma</p>
      <h1 id="home-title">Um espaço para a turma criar junto.</h1>
      <p>
        Entre com seu e-mail escolar para continuar.
      </p>
      <Link className="button button--primary login-home-link" to="/login">
        Entrar como estudante
      </Link>
      <Alert variant="info">
        As próximas áreas para estudantes serão adicionadas em etapas futuras.
      </Alert>
    </section>
  );
}
