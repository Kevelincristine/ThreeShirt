import { createBrowserRouter } from "react-router-dom";
import { App } from "./App";
import { HomePage } from "../pages/HomePage";
import { StudentLoginPage } from "../pages/StudentLoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "login", element: <StudentLoginPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);
