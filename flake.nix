{
  description = "Music2DAP — Spotify links to tagged audio for your DAP";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      music2dap = pkgs.callPackage ./packages/music2dap.nix { };
      overlay = final: prev: {
        music2dap = final.callPackage ./packages/music2dap.nix { };
      };
    in
    {
      packages.${system} = {
        default = music2dap;
        music2dap = music2dap;
      };

      overlays.default = overlay;

      nixosModules.music2dap = import ./modules/music2dap.nix { isHomeManager = false; };
      homeManagerModules.music2dap = import ./modules/music2dap.nix { isHomeManager = true; };

      formatter.${system} = pkgs.nixfmt-rfc-style;
    };
}
