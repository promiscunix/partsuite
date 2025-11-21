{
  description = "Invoice OCR + extraction dev environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";

  outputs = {
    self,
    nixpkgs,
  }: let
    system = "x86_64-linux";
    pkgs = import nixpkgs {inherit system;};
    pythonEnv = pkgs.python3.withPackages (ps: [
      ps.pypdf2 # Provides the PyPDF2 module
      ps.dateutil # python-dateutil
      ps.rich # pretty terminal output

      # NEW: API + DB stuff
      ps.fastapi
      ps.uvicorn
      ps.sqlmodel
      ps."python-multipart"
    ]);
  in {
    devShells.${system}.default = pkgs.mkShell {
      packages = [
        pythonEnv
        pkgs.python312Packages.pypdf
        pkgs.ocrmypdf
        pkgs.tesseract
        pkgs.poppler_utils # pdftotext etc
        pkgs.ghostscript
        pkgs.qpdf
        pkgs.unpaper
      ];

      shellHook = ''
        echo "Invoice dev shell ready."
        echo "Example:"
        echo "  python invoice_pipeline.py bulk_scan.pdf --output-dir out_invoices --db invoices.db"
        echo
        echo "API example:"
        echo "  uvicorn api.main:app --reload"
      '';
    };
  };
}
