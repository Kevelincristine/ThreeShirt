import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <section aria-labelledby="not-found-title" className="paper-panel">
      <p className="eyebrow">Página não encontrada</p>
      <h1 id="not-found-title">Este caminho ainda não existe.</h1>
      <Link className="text-link" to="/">
        Voltar ao início
      </Link>
    </section>
  );
}
