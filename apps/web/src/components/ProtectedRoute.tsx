import { useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuthStore } from "../stores/auth";
import { signInPath } from "../lib/persistentSession";
import { PageLoading } from "./ui/Skeleton";

export default function ProtectedRoute() {
  const { token, isLoading, checkAuth } = useAuthStore();
  const location = useLocation();

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  if (isLoading) {
    return (
      <div className="h-screen">
        <PageLoading />
      </div>
    );
  }

  if (!token) {
    const next = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to={signInPath(next)} replace />;
  }

  return <Outlet />;
}
