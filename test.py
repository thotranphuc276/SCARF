import torch

from lib.models.siren import GeoSIREN

if __name__ == '__main__':
    input_data = torch.randn(16, 3)  # Example input
    z = torch.randn(16, 1)  # Latent code for conditioning
    geo_siren = GeoSIREN(input_dim=3, z_dim=1, hidden_dim=128, output_dim=3, device='cpu')
    output = geo_siren(input_data, z)
    print(output.shape)  # Should be (batch_size, 3)
