import { NavLink } from "react-router-dom";

export interface NavbarLink {
  href: string;
  label: string;
}

interface NavbarProps {
  links: readonly NavbarLink[];
}

export function Navbar({ links }: NavbarProps) {
  return (
    <nav aria-label="Navegação principal" className="site-nav">
      <ul className="site-nav__list">
        {links.map((link) => (
          <li key={link.href}>
            <NavLink
              className={({ isActive }) =>
                isActive ? "site-nav__link is-active" : "site-nav__link"
              }
              end={link.href === "/"}
              to={link.href}
            >
              {link.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
